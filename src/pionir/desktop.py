"""Small dependency-free Windows desktop shell for the Pionir runtime."""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .bootstrap import PionirRuntime, build_runtime
from .cli import _capabilities, _doctor
from .contracts import Task


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


class DesktopController:
    """UI-independent calls so the desktop boundary remains testable."""

    def __init__(self, runtime: PionirRuntime) -> None:
        self.runtime = runtime

    def ask(self, route: str, content: str) -> Any:
        message = content.strip()
        if not message:
            raise ValueError("Enter a message first")
        if route == "Atani":
            task = Task(
                "reasoning.atani_answer",
                {"content": message},
                frozenset({"atani.chat"}),
            )
        elif route == "Atani · depth":
            task = Task(
                "reasoning.atani_depth",
                {"content": message},
                frozenset({"atani.chat"}),
            )
        elif route == "Theo":
            if "theo" not in self.runtime.adapters:
                raise ValueError("Theo is not configured; set PIONIR_THEO_TOKEN")
            task = Task("conversation.theo_reply", {"content": message})
        else:
            raise ValueError(f"Unknown desktop route: {route}")
        return dict(self.runtime.executive.execute(task).output)

    def bryo_status(self) -> Any:
        if "bryo" not in self.runtime.adapters:
            raise ValueError(
                "Bryo is not configured; set PIONIR_BRYO_STATUS_COMMAND_JSON"
            )
        return dict(
            self.runtime.executive.execute(Task("organism.bryo_status", {})).output
        )

    def autogenesis_status(self) -> Any:
        if "autogenesis" not in self.runtime.adapters:
            raise ValueError("Autogenesis is not configured")
        return dict(
            self.runtime.executive.execute(
                Task("organism.autogenesis_status", {})
            ).output
        )

    def probability_status(self) -> Any:
        if "probability" not in self.runtime.adapters:
            raise ValueError("Probability is not configured")
        return dict(
            self.runtime.executive.execute(
                Task("organism.probability_status", {})
            ).output
        )

    def genesis_status(self) -> Any:
        if "genesis" not in self.runtime.adapters:
            raise ValueError("Genesis is not configured")
        return dict(
            self.runtime.executive.execute(Task("organism.genesis_status", {})).output
        )

    def doctor(self) -> Any:
        return _doctor(self.runtime)

    def capabilities(self) -> Any:
        return _capabilities(self.runtime)


class PionirDesktop:
    def __init__(self, root: Any, controller: DesktopController) -> None:
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.root = root
        self.controller = controller
        self._events: queue.Queue[tuple[str, str, Any]] = queue.Queue()
        self._busy = False

        root.title("Pionir")
        root.geometry("1040x740")
        root.minsize(780, 560)
        root.option_add("*Font", ("Segoe UI", 10))

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(outer, text="Pionir", font=("Segoe UI Semibold", 22)).pack(
            anchor=tk.W
        )
        ttk.Label(
            outer,
            text="One console for independently governed specialist agents",
        ).pack(anchor=tk.W, pady=(0, 12))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)
        chat_tab = ttk.Frame(notebook, padding=12)
        system_tab = ttk.Frame(notebook, padding=12)
        notebook.add(chat_tab, text="Conversation")
        notebook.add(system_tab, text="System")

        route_row = ttk.Frame(chat_tab)
        route_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(route_row, text="Route").pack(side=tk.LEFT)
        self.route = ttk.Combobox(
            route_row,
            state="readonly",
            # Theo leads and is the default: Ian decided on 2026-08-30 that he
            # is Pionir's voice. It read "Theo · safe peer" for as long as that
            # was the path, because relabelling a bounded peer exchange as
            # "Theo" would have hidden the one thing worth knowing about it.
            # /voice/chat replaced it, so the plain label is now the true one.
            values=("Theo", "Atani", "Atani · depth"),
            width=24,
        )
        self.route.current(0)
        self.route.pack(side=tk.LEFT, padx=(8, 0))

        self.transcript = scrolledtext.ScrolledText(
            chat_tab,
            wrap=tk.WORD,
            state=tk.DISABLED,
            height=22,
            padx=10,
            pady=10,
        )
        self.transcript.pack(fill=tk.BOTH, expand=True)

        composer = ttk.Frame(chat_tab)
        composer.pack(fill=tk.X, pady=(10, 0))
        self.message = tk.Text(composer, height=4, wrap=tk.WORD, padx=8, pady=8)
        self.message.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.send_button = ttk.Button(composer, text="Send", command=self._send)
        self.send_button.pack(side=tk.LEFT, padx=(10, 0), fill=tk.Y)
        self.message.bind("<Control-Return>", self._send_event)

        controls = ttk.Frame(system_tab)
        controls.pack(fill=tk.X)
        ttk.Button(
            controls,
            text="Run doctor",
            command=lambda: self._submit("Doctor", self.controller.doctor),
        ).pack(side=tk.LEFT)
        ttk.Button(
            controls,
            text="Show capabilities",
            command=lambda: self._submit(
                "Capabilities", self.controller.capabilities
            ),
        ).pack(side=tk.LEFT, padx=8)
        ttk.Button(
            controls,
            text="Read Bryo status",
            command=lambda: self._submit("Bryo", self.controller.bryo_status),
        ).pack(side=tk.LEFT)
        ttk.Button(
            controls,
            text="Read Autogenesis status",
            command=lambda: self._submit(
                "Autogenesis", self.controller.autogenesis_status
            ),
        ).pack(side=tk.LEFT, padx=8)
        ttk.Button(
            controls,
            text="Read Probability status",
            command=lambda: self._submit(
                "Probability", self.controller.probability_status
            ),
        ).pack(side=tk.LEFT)
        ttk.Button(
            controls,
            text="Read Genesis status",
            command=lambda: self._submit("Genesis", self.controller.genesis_status),
        ).pack(side=tk.LEFT, padx=8)
        self.system_output = scrolledtext.ScrolledText(
            system_tab,
            wrap=tk.WORD,
            state=tk.DISABLED,
            padx=10,
            pady=10,
        )
        self.system_output.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.status = tk.StringVar(value="Ready")
        ttk.Label(outer, textvariable=self.status).pack(anchor=tk.W, pady=(8, 0))
        root.after(100, self._poll)

    @staticmethod
    def _render(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            answer = value.get("answer")
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
            snapshot = value.get("snapshot")
            if isinstance(snapshot, str) and snapshot.strip():
                return snapshot.strip()
        return json.dumps(_jsonable(value), ensure_ascii=False, indent=2, default=str)

    def _append(self, widget: Any, heading: str, value: Any) -> None:
        import tkinter as tk

        widget.configure(state=tk.NORMAL)
        widget.insert(tk.END, f"{heading}\n{self._render(value)}\n\n")
        widget.configure(state=tk.DISABLED)
        widget.see(tk.END)

    def _send_event(self, event: object) -> str:
        del event
        self._send()
        return "break"

    def _send(self) -> None:
        import tkinter as tk

        if self._busy:
            return
        content = self.message.get("1.0", tk.END).strip()
        route = self.route.get()
        if not content:
            self.status.set("Enter a message first")
            return
        self._append(self.transcript, "Ian", content)
        self.message.delete("1.0", tk.END)
        self._submit(route, lambda: self.controller.ask(route, content))

    def _submit(self, label: str, operation: Callable[[], Any]) -> None:
        if self._busy:
            self.status.set("One specialist call is already running")
            return
        self._busy = True
        self.send_button.configure(state="disabled")
        self.status.set(f"{label} is working…")

        def run() -> None:
            try:
                self._events.put(("ok", label, operation()))
            except Exception as error:  # noqa: BLE001 - UI reports isolated failures
                self._events.put(("error", label, error))

        threading.Thread(target=run, name=f"pionir-{label}", daemon=True).start()

    def _poll(self) -> None:
        try:
            kind, label, value = self._events.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        self._busy = False
        self.send_button.configure(state="normal")
        if kind == "ok":
            target = self.transcript if label in {
                "Atani",
                "Atani · depth",
                "Theo",
            } else self.system_output
            self._append(target, label, value)
            self.status.set("Ready")
        else:
            self._append(
                self.system_output,
                f"{label} error",
                {"error_type": type(value).__name__, "message": str(value)},
            )
            self.status.set(f"{label} failed safely")
        self.root.after(100, self._poll)


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as error:
        raise RuntimeError("Pionir Desktop requires Python's Tk support") from error

    root = tk.Tk()
    try:
        runtime = build_runtime()
    except Exception as error:  # noqa: BLE001 - present startup diagnostics in the GUI
        root.withdraw()
        messagebox.showerror(
            "Pionir could not start",
            f"{type(error).__name__}: {error}",
            parent=root,
        )
        root.destroy()
        return 1
    PionirDesktop(root, DesktopController(runtime))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
