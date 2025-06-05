#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import socket
import threading
import io
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QFileDialog, QSplitter, QMessageBox, QScrollArea
)
from PyQt5.QtGui import QPixmap, QImage
from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PIL import Image, ImageDraw, ImageFont
import qrcode
import barcode
from barcode.writer import ImageWriter


class SignalEmitter(QObject):
    log_signal = pyqtSignal(str)
    image_signal = pyqtSignal(QPixmap)


class ESC_POS_Parser:
    def __init__(self, on_render, on_log):
        self.buffer = bytearray()
        self.on_render = on_render
        self.on_log = on_log
        self.objects = []
        self.style = {
            "bold": False,
            "underline": False,
            "align": "left",
            "text_size": (1, 1)
        }
        self.state = "NORMAL"
        self.log_enabled = True

        # Ancho fijo del ticket
        self.paper_width = 400
        self.print_width = None  # definido pero no se emplea

        # Flag para ignorar texto imprimible hasta encontrar un LF
        self.skip_text_until_lf = False

    def feed(self, data: bytes):
        """Agrega nuevos bytes al buffer y los procesa."""
        self.buffer.extend(data)
        self._process()

    def _log_command(self, cmd_name, data=None):
        """Registra en el log los comandos ESC/POS que se van recibiendo."""
        if self.log_enabled:
            hex_data = " ".join(f"{b:02X}" for b in data) if data else ""
            if "UNKNOWN" in cmd_name:
                self.on_log(f"[CMD] {cmd_name.ljust(15)} {hex_data} - Bytes: {len(data) if data else 0}")
            else:
                self.on_log(f"[CMD] {cmd_name.ljust(15)} {hex_data}")

    def _process(self):
        """
        Recorre el buffer y:
         - Reconoce texto ASCII, saltos de línea, imágenes, barras, QR, comandos ESC/POS, etc.
         - Cuando identifica un objeto, lo agrega a self.objects.
         - Si llega a “cut” (GS V), en lugar de añadir texto “B”, genera un objeto ("cut", None).
         - Al final, llama a on_render(self.objects) y descarta los bytes procesados.
        """
        i = 0
        current_text = ""

        while i < len(self.buffer):
            b = self.buffer[i]

            # ----------------------- ESTADO NORMAL ------------------------------
            if self.state == "NORMAL":

                # 1) Si estamos ignorando texto hasta LF, descartamos bytes imprimibles
                if self.skip_text_until_lf:
                    if b == 0x0A:  # saltó la línea
                        # Procesar ese LF como si fuera un salto normal
                        if current_text.strip():
                            self.objects.append(("text", current_text.strip(), self.style.copy()))
                        current_text = ""
                        self.skip_text_until_lf = False
                        i += 1
                        continue
                    else:
                        # Cualquier otro byte simplemente se descarta
                        i += 1
                        continue

                # 2) LF = 0x0A → salto de línea
                if b == 0x0A:
                    if current_text.strip():
                        self.objects.append(("text", current_text.strip(), self.style.copy()))
                    current_text = ""
                    i += 1
                    continue

                # 3) Detectar QR (GS ( k)
                if b == 0x1D and i + 2 < len(self.buffer) and self.buffer[i + 1] == 0x28 and self.buffer[i + 2] == 0x6B:
                    try:
                        # Leer longitud
                        if i + 8 < len(self.buffer):
                            size = self.buffer[i + 7]
                            j = i + 8
                            if j + size <= len(self.buffer):
                                qr_data = self.buffer[j : j + size].decode("utf-8", errors="ignore")
                                self.objects.append(("qr", qr_data))
                                current_text = ""
                                # A partir de aquí, ignorar texto hasta el próximo LF
                                self.skip_text_until_lf = True
                                i = j + size
                                continue
                    except:
                        pass
                    # Si falla, avanzamos un byte
                    i += 1
                    continue

                # 4) Detectar CÓDIGO DE BARRAS (GS k)
                if b == 0x1D and i + 1 < len(self.buffer) and self.buffer[i + 1] == 0x6B:
                    j = i + 3
                    while j < len(self.buffer) and self.buffer[j] != 0x00:
                        j += 1
                    data = self.buffer[i + 3 : j].decode("ascii", errors="ignore")
                    self.objects.append(("barcode", data))
                    i = j + 1
                    continue

                # 5) Detectar IMAGEN (GS v 0)
                if b == 0x1D and i + 7 < len(self.buffer) and self.buffer[i + 1] == 0x76:
                    width_bytes = self.buffer[i + 4]
                    height_bytes = self.buffer[i + 6]
                    img_width = width_bytes * 8
                    img_height = height_bytes
                    data_start = i + 8
                    data_end = data_start + width_bytes * height_bytes
                    if data_end <= len(self.buffer):
                        raw_image = self.buffer[data_start:data_end]
                        img = Image.new('1', (img_width, img_height))
                        pixels = img.load()
                        for yy in range(img_height):
                            for xx in range(width_bytes):
                                byte = raw_image[yy * width_bytes + xx]
                                for bit in range(8):
                                    pixels[xx * 8 + bit, yy] = 255 * (
                                        not (byte & (1 << (7 - bit)))
                                    )
                        self.objects.append(("image", img.convert("L")))
                        i = data_end
                        continue
                    else:
                        break  # aún no llegó todo el bloque de bits

                # 6) Texto ASCII imprimible
                if 32 <= b <= 126:
                    current_text += chr(b)
                    i += 1
                    continue

                # 7) ESC (0x1B)
                if b == 0x1B:
                    self.state = "ESC"
                    i += 1
                    continue
                #    GS (0x1D)
                elif b == 0x1D:
                    self.state = "GS"
                    i += 1
                    continue

                # 8) Cualquier otro byte: avanzamos
                i += 1
                continue

            # ------------------------ ESTADO ESC -------------------------------
            elif self.state == "ESC":
                cmd = self.buffer[i]

                # 1B 40 = INIT
                if cmd == 0x40:
                    self._log_command("INIT", self.buffer[i - 1 : i + 1])
                    self.state = "NORMAL"
                    i += 1
                    continue

                # 1B 45 n = BOLD
                elif cmd == 0x45 and i + 1 < len(self.buffer):
                    self.style["bold"] = (self.buffer[i + 1] != 0)
                    self._log_command("BOLD", self.buffer[i - 1 : i + 2])
                    self.state = "NORMAL"
                    i += 2
                    continue

                # 1B 61 n = ALIGN
                elif cmd == 0x61 and i + 1 < len(self.buffer):
                    align = self.buffer[i + 1]
                    opciones = {0: "left", 1: "center", 2: "right"}
                    self.style["align"] = opciones.get(align, "left")
                    self._log_command("ALIGN", self.buffer[i - 1 : i + 2])
                    self.state = "NORMAL"
                    i += 2
                    continue

                # 1B 64 n = FEED
                elif cmd == 0x64 and i + 1 < len(self.buffer):
                    lines = self.buffer[i + 1]
                    self._log_command("FEED", self.buffer[i - 1 : i + 2])
                    for _ in range(lines):
                        if current_text.strip():
                            self.objects.append(("text", current_text.strip(), self.style.copy()))
                        current_text = ""
                        self.objects.append(("feed", 1))
                    self.state = "NORMAL"
                    i += 2
                    continue

                # 1B 2D n = UNDERLINE
                elif cmd == 0x2D and i + 1 < len(self.buffer):
                    self.style["underline"] = (self.buffer[i + 1] != 0)
                    self._log_command("UNDERLINE", self.buffer[i - 1 : i + 2])
                    self.state = "NORMAL"
                    i += 2
                    continue

                # 1B 74 n = CODEPAGE (solo registramos)
                elif cmd == 0x74 and i + 1 < len(self.buffer):
                    self._log_command("CODEPAGE", self.buffer[i - 1 : i + 2])
                    self.state = "NORMAL"
                    i += 2
                    continue

                else:
                    # ESC desconocido
                    self._log_command(f"UNKNOWN ESC {cmd:02X}", self.buffer[i - 1 : i + 1])
                    self.state = "NORMAL"
                    i += 1
                    continue

            # ------------------------ ESTADO GS --------------------------------
            elif self.state == "GS":
                cmd = self.buffer[i]

                # -- Nuevo bloque: detectar GS W (1D 57) y consumir sus 2 parámetros --
                if cmd == 0x57 and i + 2 < len(self.buffer):
                    # Logueamos “GS W” junto con sus 2 bytes de parámetro (por ejemplo 30 02)
                    self._log_command("GS W", self.buffer[i - 1 : i + 3])
                    # Avanzar sobretodo para que no caiga 0x30 como texto
                    i += 3
                    self.state = "NORMAL"
                    continue

                # 1D 21 n = TEXT SIZE
                if cmd == 0x21 and i + 1 < len(self.buffer):
                    size = self.buffer[i + 1]
                    width = (size >> 4) + 1
                    height = (size & 0x0F) + 1
                    self.style["text_size"] = (width, height)
                    self._log_command("TEXT SIZE", self.buffer[i - 1 : i + 2])
                    self.state = "NORMAL"
                    i += 2
                    continue

                # 1D 56 m = CUT PAPER  <---  aquí detectamos el “cut”
                elif cmd == 0x56 and i + 1 < len(self.buffer):
                    m = self.buffer[i + 1]
                    # Simular el corte: en lugar de dejar caer 'B', creamos un objeto “cut”
                    self.objects.append(("cut", None))
                    self._log_command("CUT", self.buffer[i - 1 : i + 2])
                    self.state = "NORMAL"
                    i += 2
                    continue

                else:
                    # GS desconocido
                    self._log_command(f"UNKNOWN GS {cmd:02X}", self.buffer[i - 1 : i + 1])
                    self.state = "NORMAL"
                    i += 1
                    continue

        # Si quedó texto pendiente, lo agregamos
        if current_text.strip():
            self.objects.append(("text", current_text.strip(), self.style.copy()))

        # Si hay objetos, llamamos al callback para renderizar
        if self.objects:
            self.on_render(self.objects)
            self.objects.clear()

        # Eliminamos los bytes ya procesados del buffer
        if i > 0:
            del self.buffer[:i]


class TCPServer(threading.Thread):
    def __init__(self, host, port, on_data_received, on_log):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.on_data_received = on_data_received
        self.on_log = on_log
        self.sock = None

    def run(self):
        """Levanta el socket TCP y acepta clientes en bucle."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.host, self.port))
            self.sock.listen(5)
            self.on_log(f"[{datetime.now().strftime('%H:%M:%S')}] Servidor escuchando en {self.host}:{self.port}")
            while True:
                client, addr = self.sock.accept()
                threading.Thread(target=self.handle_client, args=(client,), daemon=True).start()
        except Exception as e:
            self.on_log(f"[ERROR] {str(e)}")
        finally:
            if self.sock:
                self.sock.close()

    def handle_client(self, client_sock):
        """Manejo de cada cliente: recibe datos y los reenvía al parser."""
        with client_sock:
            try:
                client_info = f"{client_sock.getpeername()[0]}:{client_sock.getpeername()[1]}"
                self.on_log(f"[{datetime.now().strftime('%H:%M:%S')}] Conexión establecida con {client_info}")

                while True:
                    data = client_sock.recv(4096)
                    if not data:
                        self.on_log(f"[{datetime.now().strftime('%H:%M:%S')}] Cliente {client_info} cerró la conexión")
                        break

                    hexdata = " ".join(f"{b:02X}" for b in data)
                    self.on_log(f"[{datetime.now().strftime('%H:%M:%S')}] [⇢] Datos recibidos ({len(data)} bytes)")
                    self.on_log(f"[HEX] {hexdata}")
                    self.on_data_received(data)

            except Exception as e:
                self.on_log(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] Excepción en cliente {client_info}: {str(e)}")
            finally:
                self.on_log(f"[{datetime.now().strftime('%H:%M:%S')}] Cliente {client_info} desconectado")


class PrinterSimulator(QWidget):
    def __init__(self):
        super().__init__()
        self.host = "0.0.0.0"
        self.port = 9100
        # Lista de PIL.Images: mantiene todos los tickets recibidos
        self.all_tickets = []
        self.ticket_image = None  # Aquí guardamos la imagen combinada de todos los tickets
        self.signal_emitter = SignalEmitter()
        self.signal_emitter.log_signal.connect(self._update_log)
        self.signal_emitter.image_signal.connect(self._update_image)
        self.escpos_parser = ESC_POS_Parser(self._render_ticket_image, self._emit_log)
        self._build_ui()
        self._start_server()

    def _build_ui(self):
        """Construye la interfaz gráfica, incluyendo la entrada de ancho."""
        self.setWindowTitle("🖨️ Simulador Impresora ESC/POS")
        self.resize(1000, 600)

        layout = QVBoxLayout()
        top_bar = QHBoxLayout()

        # Inputs para IP y Puerto
        self.ip_input = QLineEdit(self.host)
        self.port_input = QLineEdit(str(self.port))

        # Input para modificar el ancho de papel (paper_width)
        self.width_input = QLineEdit(str(self.escpos_parser.paper_width))
        self.width_input.setFixedWidth(60)
        self.width_input.setToolTip("Ancho de ticket en píxeles")
        width_label = QLabel("Ancho:")

        apply_btn = QPushButton("Aplicar")
        apply_btn.clicked.connect(self._on_apply_clicked)

        top_bar.addWidget(QLabel("IP:"))
        top_bar.addWidget(self.ip_input)
        top_bar.addWidget(QLabel("Puerto:"))
        top_bar.addWidget(self.port_input)
        top_bar.addSpacing(20)
        top_bar.addWidget(width_label)
        top_bar.addWidget(self.width_input)
        top_bar.addWidget(QLabel("px"))
        top_bar.addStretch(1)
        top_bar.addWidget(apply_btn)

        # Splitter que contendrá el log a la izquierda y la vista del ticket a la derecha
        splitter = QSplitter(Qt.Horizontal)

        # Panel de LOG
        self.log_label = QLabel()
        self.log_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.log_label.setWordWrap(True)
        log_scroll = QScrollArea()
        log_scroll.setWidgetResizable(True)
        log_scroll.setWidget(self.log_label)

        # Panel de TICKET (donde se mostrará la imagen combinada)
        self.ticket_label = QLabel()
        self.ticket_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        ticket_scroll = QScrollArea()
        ticket_scroll.setWidgetResizable(True)
        ticket_scroll.setWidget(self.ticket_label)

        splitter.addWidget(log_scroll)
        splitter.addWidget(ticket_scroll)

        # Barra de botones: Guardar PNG, Guardar PDF y Reset
        button_bar = QHBoxLayout()
        save_png_btn = QPushButton("Guardar PNG")
        save_png_btn.clicked.connect(self._save_png)
        save_pdf_btn = QPushButton("Guardar PDF")
        save_pdf_btn.clicked.connect(self._save_pdf)
        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self._on_reset_clicked)
        button_bar.addStretch(1)
        button_bar.addWidget(save_png_btn)
        button_bar.addWidget(save_pdf_btn)
        button_bar.addWidget(reset_btn)

        # Montamos los layouts en el orden deseado
        layout.addLayout(top_bar)
        layout.addWidget(splitter)
        layout.addLayout(button_bar)
        self.setLayout(layout)

    def _on_apply_clicked(self):
        """
        Cuando el usuario hace clic en 'Aplicar':
        - Reinicia el servidor TCP si cambió IP o Puerto.
        - Actualiza el ancho de papel (paper_width).
        """
        # 1) Actualizar host y puerto
        self.host = self.ip_input.text().strip() or "0.0.0.0"
        try:
            self.port = int(self.port_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Puerto inválido", "Debe ser un número entero.")
            return

        # 2) Actualizar ancho
        try:
            nuevo_ancho = int(self.width_input.text().strip())
            if nuevo_ancho <= 0:
                raise ValueError("El ancho debe ser mayor a 0.")
            self.escpos_parser.paper_width = nuevo_ancho
            self._emit_log(f"Se actualizó ancho de ticket a {nuevo_ancho} px.")
        except Exception:
            QMessageBox.warning(self, "Ancho inválido", "Ingresa un número entero positivo para el ancho.")
            return

        # 3) Reiniciar servidor
        if hasattr(self, "server_thread") and self.server_thread.is_alive():
            try:
                self.server_thread.sock.close()
            except:
                pass
        self._start_server()

    def _start_server(self):
        """Inicia el hilo del servidor TCP."""
        self.server_thread = TCPServer(
            self.host, self.port,
            self._on_data_received, self._emit_log
        )
        self.server_thread.start()

    def _on_reset_clicked(self):
        """
        Vacía todo el buffer de la impresora, borra la lista de tickets,
        limpia la consola de logs y deja la pantalla lista para recibir nuevos tickets.
        """
        # 1) Limpiar buffer y objetos pendientes del parser
        self.escpos_parser.buffer.clear()
        self.escpos_parser.objects.clear()

        # 2) Limpiar lista de imágenes de tickets
        self.all_tickets.clear()

        # 3) Limpiar imagen en pantalla
        self.ticket_label.clear()
        self.ticket_image = None

        # 4) Limpiar la consola de logs
        self.log_label.clear()

        # 5) Agregar mensaje de confirmación (opcional)
        self._emit_log("Se ha reseteado el simulador y limpiado todos los tickets y logs.")

    def _emit_log(self, message: str):
        """Emite un mensaje al panel de log."""
        self.signal_emitter.log_signal.emit(message)

    def _update_log(self, message: str):
        """Actualiza el QLabel que contiene el texto del log."""
        current = self.log_label.text()
        self.log_label.setText(current + message + "\n")

    def _on_data_received(self, data: bytes):
        """
        Callback que recibe los bytes entrantes y los pasa al parser ESC_POS.
        """
        self.escpos_parser.feed(data)

    def _update_image(self, pixmap: QPixmap):
        """Actualiza la vista del ticket en la interfaz."""
        self.ticket_label.setPixmap(pixmap)

    def _render_ticket_image(self, elements):
        """
        A partir de la lista de elementos [(tipo, contenido, estilo), ...], renderiza
        un PIL.Image con todo el ticket y luego lo convierte a QPixmap para mostrarlo.
        Además, acumula cada ticket en self.all_tickets y muestra todos juntos.
        """
        width = self.escpos_parser.paper_width
        padding = 10  # margen horizontal y vertical

        # --- 1ª parte: construir la imagen del ticket recién llegado ---
        y = padding

        # Cargamos fuentes (DejaVu es estándar en muchas distribuciones)
        try:
            font_regular = ImageFont.truetype("DejaVuSans.ttf", 20)
            font_bold = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
        except:
            font_regular = font_bold = ImageFont.load_default()
            if not hasattr(font_regular, 'getbbox'):
                font_regular.getbbox = lambda text: (0, 0, len(text) * 6, 10)
                font_bold.getbbox = font_regular.getbbox

        # Primer pase: calcular altura necesaria para este ticket
        total_height = padding * 2
        for el in elements:
            tipo = el[0]
            if tipo == "text":
                font_height = 20 * el[2]["text_size"][1]
                total_height += font_height + 10
            elif tipo == "image":
                total_height += el[1].height + 10
            elif tipo == "barcode":
                total_height += 50 + 10  # asumimos 50 px de alto
            elif tipo == "qr":
                total_height += 200 + 10  # asumimos 200×200 para QR
            elif tipo == "cut":
                total_height += 10  # 10 px para la línea de corte
            elif tipo == "feed":
                total_height += 30 * el[1]

        # Creamos la imagen de este ticket (modo “L” = blanco y negro)
        image = Image.new("L", (width, total_height), 255)
        draw = ImageDraw.Draw(image)

        # Segundo pase: dibujar en la imagen del ticket
        y = padding
        for el in elements:
            tipo = el[0]

            if tipo == "text":
                text, style = el[1], el[2]
                font_size = 20 * style["text_size"][0]
                try:
                    font = ImageFont.truetype(
                        "DejaVuSans-Bold.ttf" if style.get("bold") else "DejaVuSans.ttf",
                        font_size
                    )
                except:
                    font = font_bold if style.get("bold") else font_regular
                    if not hasattr(font, 'getbbox'):
                        font.getbbox = lambda t: (0, 0, len(t) * 6, font_size)

                bbox = font.getbbox(text)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]

                # Calcular X según alineación
                if style["align"] == "center":
                    x = (width - text_width) // 2
                elif style["align"] == "right":
                    x = width - text_width - padding
                else:
                    x = padding

                draw.text((x, y), text, font=font, fill=0)
                if style.get("underline"):
                    draw.line(
                        (x, y + text_height + 2, x + text_width, y + text_height + 2),
                        fill=0
                    )
                y += text_height + 10

            elif tipo == "qr":
                # Generar el QR y pegarlo (200×200) sin imprimir nunca el texto “k1Q0”
                try:
                    qr_img = qrcode.make(el[1]).resize((200, 200))
                except Exception as e:
                    self._emit_log(f"[QR ERROR] {e}")
                    qr_img = Image.new("L", (200, 200), 255)

                x_pos = (width - qr_img.width) // 2
                image.paste(qr_img, (x_pos, y))
                y += qr_img.height + 10

            elif tipo == "barcode":
                # Generar el código de barras
                try:
                    CODE128 = barcode.get_barcode_class("code128")
                    code = CODE128(el[1], writer=ImageWriter())
                    buffer = io.BytesIO()
                    code.write(buffer, {"module_height": 10.0, "font_size": 10})
                    bar_img = Image.open(buffer).convert("L")
                except Exception as e:
                    self._emit_log(f"[BARCODE ERROR] {e}")
                    bar_img = Image.new("L", (200, 50), 255)

                x_pos = (width - bar_img.width) // 2
                image.paste(bar_img, (x_pos, y))
                y += bar_img.height + 10

            elif tipo == "image":
                img = el[1]
                # Si la imagen es más ancha que el ticket, la escalamos
                if img.width > width:
                    scale = width / img.width
                    new_height = int(img.height * scale)
                    img = img.resize((width, new_height), Image.LANCZOS)
                x_pos = (width - img.width) // 2
                image.paste(img, (x_pos, y))
                y += img.height + 10

            elif tipo == "cut":
                # Dibujar una línea horizontal para simular el corte
                x0 = padding
                x1 = width - padding
                draw.line((x0, y + 2, x1, y + 2), fill=0, width=2)
                y += 10

            elif tipo == "feed":
                y += 30 * el[1]

        # --- 2ª parte: agregar este ticket recién generado a la lista ---
        self.all_tickets.append(image)

        # --- 3ª parte: generar la imagen combinada de todos los tickets ---
        # Calculamos la altura total para apilar todos los tickets
        gap = 10  # espacio vertical entre cada ticket
        combined_height = gap  # margen superior
        for t_img in self.all_tickets:
            combined_height += t_img.height + gap

        combined_img = Image.new("L", (width, combined_height), 255)
        y_offset = gap
        for t_img in self.all_tickets:
            combined_img.paste(t_img, (0, y_offset))
            y_offset += t_img.height + gap

        # Guardamos la imagen combinada para exportarla o mostrarla
        self.ticket_image = combined_img

        # Convertimos la imagen combinada a QPixmap y la emitimos para la UI
        buf = io.BytesIO()
        combined_img.save(buf, format="PNG")
        qimg = QImage.fromData(buf.getvalue())
        self.signal_emitter.image_signal.emit(QPixmap.fromImage(qimg))

    def _render_barcode(self, data: str) -> Image.Image:
        """
        Genera un PIL.Image con un código de barras. (No se usa directamente aquí.)
        """
        try:
            CODE128 = barcode.get_barcode_class("code128")
            code = CODE128(data, writer=ImageWriter())
            buffer = io.BytesIO()
            code.write(buffer, {"module_height": 10.0, "font_size": 10})
            return Image.open(buffer).convert("L")
        except Exception as e:
            self._emit_log(f"[BARCODE ERROR] {e}")
            return Image.new("L", (200, 50), 255)

    def _save_png(self):
        """
        Guarda la imagen actual (toda la pila de tickets) como PNG.
        Si no hay imagen, muestra un aviso.
        """
        if self.ticket_image is None:
            QMessageBox.warning(self, "Sin ticket", "No hay ningún ticket para guardar.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar tickets como PNG",
            "",
            "Archivos PNG (*.png)"
        )
        if not path:
            return

        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            self.ticket_image.save(path, "PNG")
            QMessageBox.information(self, "Éxito", f"Imagen guardada en:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Error al guardar PNG", str(e))

    def _save_pdf(self):
        """
        Guarda la imagen actual (toda la pila de tickets) como PDF.
        Convertimos a RGB antes de exportar.
        """
        if self.ticket_image is None:
            QMessageBox.warning(self, "Sin ticket", "No hay ningún ticket para guardar.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar tickets como PDF",
            "",
            "Archivos PDF (*.pdf)"
        )
        if not path:
            return

        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            img_to_save = self.ticket_image.convert("RGB")
            img_to_save.save(path, "PDF", resolution=100.0)
            QMessageBox.information(self, "Éxito", f"Archivo PDF guardado en:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Error al guardar PDF", str(e))

    def _emit_log(self, message: str):
        """Emite un mensaje al panel de log."""
        self.signal_emitter.log_signal.emit(message)

    def _update_image(self, pixmap: QPixmap):
        """Actualiza la vista del ticket en la interfaz."""
        self.ticket_label.setPixmap(pixmap)


def main():
    app = QApplication(sys.argv)
    window = PrinterSimulator()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
