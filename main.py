import os
import subprocess
import tkinter as tk
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from math import ceil
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Lock
from time import monotonic

from PIL import Image, ImageGrab, ImageTk


def select_region(screenshot: Image.Image) -> tuple[int, int, int, int] | None:
    window = tk.Tk()
    try:
        window.title("Поиск по скриншоту")
        window.attributes("-fullscreen", True)
        window.attributes("-topmost", True)

        canvas = tk.Canvas(
            window, background="#161616", highlightthickness=0, cursor="crosshair"
        )
        canvas.pack(fill=tk.BOTH, expand=True)
        canvas.wait_visibility()

        canvas_width = canvas.winfo_width()
        canvas_height = canvas.winfo_height()
        with screenshot.copy() as preview:
            preview.thumbnail((canvas_width, canvas_height), Image.Resampling.LANCZOS)
            preview_width, preview_height = preview.size
            preview_image = ImageTk.PhotoImage(preview, master=window)

        preview_x = (canvas_width - preview_width) // 2
        preview_y = (canvas_height - preview_height) // 2
        preview_right = preview_x + preview_width
        preview_bottom = preview_y + preview_height
        canvas.create_image(preview_x, preview_y, image=preview_image, anchor="nw")

        canvas.create_rectangle(
            canvas_width // 2 - 190, 12, canvas_width // 2 + 190, 48,
            fill="#161616", outline="",
        )
        canvas.create_text(
            canvas_width // 2, 30, fill="white",
            text="Выделите область мышью. Esc — отмена.",
        )
        outline = canvas.create_rectangle(0, 0, 0, 0, outline="#62d9ff", width=2, state="hidden")
        start: tuple[int, int] | None = None
        selection: tuple[int, int, int, int] | None = None

        def drag_bounds(origin: tuple[int, int], event: tk.Event) -> tuple[int, int, int, int]:
            start_x, start_y = origin
            end_x = max(preview_x, min(event.x, preview_right))
            end_y = max(preview_y, min(event.y, preview_bottom))
            return (
                min(start_x, end_x), min(start_y, end_y),
                max(start_x, end_x), max(start_y, end_y),
            )

        def start_drag(event: tk.Event) -> None:
            nonlocal start
            if preview_x <= event.x < preview_right and preview_y <= event.y < preview_bottom:
                start = (event.x, event.y)
                canvas.coords(outline, event.x, event.y, event.x, event.y)
                canvas.itemconfigure(outline, state="normal")

        def update_drag(event: tk.Event) -> None:
            if start is not None:
                canvas.coords(outline, *drag_bounds(start, event))

        def finish_drag(event: tk.Event) -> None:
            nonlocal start, selection
            if start is None:
                return

            left, top, right, bottom = drag_bounds(start, event)
            start = None
            if left == right or top == bottom:
                canvas.itemconfigure(outline, state="hidden")
                return

            selection = (
                (left - preview_x) * screenshot.width // preview_width,
                (top - preview_y) * screenshot.height // preview_height,
                ceil((right - preview_x) * screenshot.width / preview_width),
                ceil((bottom - preview_y) * screenshot.height / preview_height),
            )
            window.quit()

        canvas.bind("<ButtonPress-1>", start_drag)
        canvas.bind("<B1-Motion>", update_drag)
        canvas.bind("<ButtonRelease-1>", finish_drag)
        window.bind("<Escape>", lambda _event: window.quit())
        window.protocol("WM_DELETE_WINDOW", window.quit)
        canvas.focus_force()
        window.mainloop()
        return selection
    finally:
        window.destroy()


def capture_region_png() -> bytes | None:
    with ImageGrab.grab(all_screens=True) as screenshot:
        region = select_region(screenshot)
        if region is None:
            return None

        with screenshot.crop(region) as cropped, BytesIO() as png_buffer:
            cropped.save(png_buffer, format="PNG")
            return png_buffer.getvalue()


LENS_UPLOAD_PAGE = """<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>Поиск по скриншоту</title>
</head>
<body>
    <p id="status" role="status">Открываем результаты Google Lens…</p>
    <form action="https://lens.google.com/v3/upload?hl=ru" method="post"
          enctype="multipart/form-data" hidden>
        <input type="file" name="encoded_image" required>
    </form>
    <script>
        async function submitScreenshot() {
            const response = await fetch("image", { cache: "no-store" });
            if (!response.ok) {
                throw new Error("Снимок недоступен.");
            }

            const image = await response.blob();
            const transfer = new DataTransfer();
            transfer.items.add(new File([image], "screenshot.png", { type: "image/png" }));

            const form = document.querySelector("form");
            form.elements.namedItem("encoded_image").files = transfer.files;
            form.requestSubmit();
        }

        submitScreenshot().catch(() => {
            document.querySelector("#status").textContent =
                "Не удалось получить снимок. Запустите поиск ещё раз.";
        });
    </script>
</body>
</html>
""".encode("utf-8")


def find_chrome() -> Path:
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            executable = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if executable.is_file():
                return executable

    raise FileNotFoundError("Google Chrome не найден в стандартных папках установки.")


def open_lens_in_chrome(screenshot_png: bytes) -> None:
    chrome = find_chrome()
    search_path = f"/{token_urlsafe(24)}/"
    image_path = search_path + "image"
    delivered = Event()
    delivery_lock = Lock()

    class SearchRequest(BaseHTTPRequestHandler):
        timeout = 4

        def do_GET(self) -> None:
            if self.headers.get("Host") != browser_host:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            if self.path == search_path:
                self.send_content("text/html; charset=utf-8", LENS_UPLOAD_PAGE)
            elif self.path == image_path:
                with delivery_lock:
                    png = screenshot_png
                    if delivered.is_set() or monotonic() >= deadline or not png:
                        self.send_error(HTTPStatus.NOT_FOUND)
                    elif self.send_content("image/png", png):
                        delivered.set()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def send_content(self, content_type: str, content: bytes) -> bool:
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.end_headers()
                self.wfile.write(content)
            except (ConnectionError, TimeoutError):
                return False
            return True

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), SearchRequest) as server:
        browser_host = f"127.0.0.1:{server.server_port}"
        deadline = monotonic() + 8
        try:
            subprocess.Popen([str(chrome), f"http://{browser_host}{search_path}"])

            while not delivered.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Chrome не получил снимок за 8 секунд.")

                server.timeout = min(remaining, 0.05)
                server.handle_request()
        finally:
            screenshot_png = b""


def main() -> None:
    try:
        screenshot_png = capture_region_png()
    except (OSError, tk.TclError) as error:
        raise SystemExit(f"Не удалось сделать снимок экрана: {error}") from error

    if screenshot_png is None:
        print("Выделение отменено.")
        return

    try:
        open_lens_in_chrome(screenshot_png)
    except OSError as error:
        raise SystemExit(f"Не удалось передать снимок в Chrome: {error}") from error
    finally:
        del screenshot_png

    print("Снимок передан в Chrome. Результаты откроются в этой вкладке.")


if __name__ == "__main__":
    main()
