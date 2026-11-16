"""Small always-on-top laptop window for typing into the VR browser.

Runs tkinter in its own thread; every tk call stays in that thread. Actions are handed to the
server's asyncio loop with run_coroutine_threadsafe.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable


class Actions:
    """What the window can ask the server to do (all coroutines, run on the server loop)."""

    def __init__(self, type_text: Callable[[str], Awaitable], go: Callable[[str], Awaitable],
                 back: Callable[[], Awaitable], home: Callable[[], Awaitable],
                 play_vr: Callable[[], Awaitable], status: Callable[[], str],
                 new_window: Callable[[], Awaitable] | None = None) -> None:
        self.type_text, self.go, self.back, self.home, self.play_vr, self.status = \
            type_text, go, back, home, play_vr, status
        self.new_window = new_window


def start(loop: asyncio.AbstractEventLoop, actions: Actions) -> threading.Thread:
    t = threading.Thread(target=_run, args=(loop, actions), daemon=True, name="vrb-keyboard")
    t.start()
    return t


def _run(loop: asyncio.AbstractEventLoop, a: Actions) -> None:
    try:
        import tkinter as tk
    except Exception as e:
        print(f"[keyboard] no tkinter ({e}); typing window not shown")
        return
    try:
        root = tk.Tk()
    except Exception as e:
        print(f"[keyboard] could not open the typing window: {e}")
        return

    def fire(coro_fn, *args) -> None:
        try:
            asyncio.run_coroutine_threadsafe(coro_fn(*args), loop)
        except Exception as e:
            print(f"[keyboard] {e!r}")

    root.title("VR Browser keyboard")
    root.attributes("-topmost", True)
    root.configure(bg="#14161c")
    root.geometry("+40+40")
    root.resizable(True, False)
    fg, bg2 = "#e8e8f0", "#232733"
    font = ("Segoe UI", 12)

    def label(text, size=10, color="#9aa3b5"):
        return tk.Label(root, text=text, bg="#14161c", fg=color, font=("Segoe UI", size), anchor="w", justify="left")

    label("Type here, press Enter: the text goes into the field you clicked in VR").pack(fill="x", padx=10, pady=(10, 2))
    e_text = tk.Entry(root, font=font, bg=bg2, fg=fg, insertbackground=fg, width=46, relief="flat")
    e_text.pack(fill="x", padx=10, ipady=5)

    def send_text(_=None):
        s = e_text.get()
        if s:
            fire(a.type_text, s)
            e_text.delete(0, "end")
    e_text.bind("<Return>", send_text)

    label("Go to address / search (in the active window)").pack(fill="x", padx=10, pady=(10, 2))
    e_url = tk.Entry(root, font=font, bg=bg2, fg=fg, insertbackground=fg, width=46, relief="flat")
    e_url.pack(fill="x", padx=10, ipady=5)

    def go(_=None):
        s = e_url.get().strip()
        if s:
            fire(a.go, s)
            e_url.delete(0, "end")
    e_url.bind("<Return>", go)

    row = tk.Frame(root, bg="#14161c")
    row.pack(fill="x", padx=10, pady=10)
    buttons = [("Back", a.back), ("Home (YouTube)", a.home)]
    if a.new_window:
        buttons.append(("New window", a.new_window))
    buttons.append(("Play in VR", a.play_vr))
    for text, fn in buttons:
        tk.Button(row, text=text, command=lambda f=fn: fire(f), bg="#2f81f7" if text == "Play in VR" else bg2,
                  fg="#ffffff", activebackground="#3d8ef8", relief="flat", font=("Segoe UI", 11),
                  padx=10, pady=4).pack(side="left", padx=(0, 8))

    status = label("", 9, "#7f8899")
    status.pack(fill="x", padx=10)
    label("Sign-in mode: stop this server, run vr_browser_test\\login.bat, sign in in the\n"
          "Brave window that opens, close it, then start this server again.", 9, "#7f8899").pack(fill="x", padx=10, pady=(4, 10))

    def tick():
        try:
            status.configure(text=a.status()[:90])
        except Exception:
            pass
        root.after(1000, tick)
    tick()
    e_text.focus_set()
    try:
        root.mainloop()
    except Exception as e:
        print(f"[keyboard] window closed: {e!r}")
