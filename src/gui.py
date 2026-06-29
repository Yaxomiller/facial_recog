from datetime import date
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from src.auth import (
    authenticate_admin,
    ensure_admin_auth_config,
    get_admin_username,
    get_auth_status,
    reset_admin_credentials,
    setup_admin_credentials,
)
from src.db import init_database, list_attendance, list_people
from src.enrollment import enroll_person
from src.exporter import export_attendance_to_excel, export_today
from src.person_manager import delete_person
from src.v2.camera_recognition import recognize_and_mark_v2
from src.storage import ensure_directories
from src.training import train_model


class AttendanceApp:
    def __init__(self) -> None:
        ensure_directories()
        init_database()
        ensure_admin_auth_config(allow_bootstrap=True)
        self.root = tk.Tk()
        self.root.title("Facial Recognition Attendance System")
        self.root.geometry("960x640")
        self.root.minsize(880, 560)
        self.status_var = tk.StringVar(value="Ready")
        self.is_task_running = False
        self.action_buttons: list[ttk.Button] = []
        self._build_login()

    def _clear_root(self) -> None:
        for widget in self.root.winfo_children():
            widget.destroy()

    def _build_login(self) -> None:
        self._clear_root()
        frame = ttk.Frame(self.root, padding=32)
        frame.pack(expand=True)

        ttk.Label(frame, text="Admin Login", font=("Segoe UI", 20, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 16))
        ttk.Label(frame, text="Username").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Label(frame, text="Password").grid(row=2, column=0, sticky="w", pady=6)

        username_entry = ttk.Entry(frame, width=30)
        password_entry = ttk.Entry(frame, width=30, show="*")
        username_entry.grid(row=1, column=1, pady=6)
        password_entry.grid(row=2, column=1, pady=6)

        configured_user = get_admin_username() or ""
        auth_status = get_auth_status()
        if configured_user:
            username_entry.insert(0, configured_user)
            ttk.Label(frame, text=f"Configured admin user: {configured_user}").grid(row=3, column=0, columnspan=2, pady=(4, 16))
        else:
            ttk.Label(frame, text="No admin credentials yet. Create them before login.").grid(row=3, column=0, columnspan=2, pady=(4, 16))

        def submit_login() -> None:
            username = username_entry.get().strip()
            password = password_entry.get()
            if authenticate_admin(username, password):
                self._build_dashboard()
            else:
                messagebox.showerror("Login Failed", "Invalid admin credentials.")

        ttk.Button(frame, text="Login", command=submit_login).grid(row=4, column=0, columnspan=2, pady=10)
        ttk.Button(frame, text="Create Credentials", command=self._setup_credentials).grid(row=5, column=0, pady=6, sticky="ew")
        if auth_status.configured:
            ttk.Button(frame, text="Reset Credentials", command=self._reset_credentials).grid(row=5, column=1, pady=6, sticky="ew")
        self.root.bind("<Return>", lambda _event: submit_login())

    def _setup_credentials(self) -> None:
        if get_auth_status().configured:
            messagebox.showinfo("Credentials Exist", "Credentials are already configured. Use reset instead.")
            return

        username = simpledialog.askstring("Create Credentials", "Choose username:", parent=self.root)
        if not username:
            return
        password = simpledialog.askstring("Create Credentials", "Choose password:", parent=self.root, show="*")
        if not password:
            return
        confirm = simpledialog.askstring("Create Credentials", "Confirm password:", parent=self.root, show="*")
        if confirm != password:
            messagebox.showerror("Setup Failed", "Passwords do not match.")
            return
        try:
            setup_admin_credentials(username=username, password=password)
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror("Setup Failed", str(exc))
            return
        messagebox.showinfo("Success", "Credentials created successfully.")
        self._build_login()

    def _reset_credentials(self) -> None:
        username = simpledialog.askstring("Reset Credentials", "Username:", parent=self.root)
        if not username:
            return
        new_password = simpledialog.askstring("Reset Credentials", "New password:", parent=self.root, show="*")
        if not new_password:
            return
        confirm = simpledialog.askstring("Reset Credentials", "Confirm new password:", parent=self.root, show="*")
        if confirm != new_password:
            messagebox.showerror("Reset Failed", "Passwords do not match.")
            return
        try:
            reset_admin_credentials(
                username=username,
                new_password=new_password,
            )
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror("Reset Failed", str(exc))
            return
        messagebox.showinfo("Success", "Credentials reset successfully.")
        self._build_login()

    def _build_dashboard(self) -> None:
        self._clear_root()
        self.root.unbind("<Return>")

        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(1, weight=1)

        ttk.Label(container, text="Attendance Dashboard", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(container, textvariable=self.status_var).grid(row=0, column=1, sticky="e")

        actions = ttk.LabelFrame(container, text="Actions", padding=16)
        actions.grid(row=1, column=0, sticky="nsw", padx=(0, 16))

        self.action_buttons = []

        enroll_button = ttk.Button(actions, text="Enroll Person", command=self._enroll_from_dialog)
        enroll_button.pack(fill="x", pady=6)
        self.action_buttons.append(enroll_button)

        delete_button = ttk.Button(actions, text="Delete Selected Person", command=self._delete_selected_person)
        delete_button.pack(fill="x", pady=6)
        self.action_buttons.append(delete_button)

        train_button = ttk.Button(
            actions,
            text="Train Model",
            command=lambda: self._run_async(train_model, "Training model...", self._show_training_result),
        )
        train_button.pack(fill="x", pady=6)
        self.action_buttons.append(train_button)

        recognition_button = ttk.Button(
            actions,
            text="Start Recognition",
            command=lambda: self._run_async(
                recognize_and_mark_v2,
                "Recognition running. Press 'q', 'Esc', or close the camera window to stop.",
            ),
        )
        recognition_button.pack(fill="x", pady=6)
        self.action_buttons.append(recognition_button)

        export_today_button = ttk.Button(actions, text="Export Today to Excel", command=self._export_today)
        export_today_button.pack(fill="x", pady=6)
        self.action_buttons.append(export_today_button)

        export_range_button = ttk.Button(actions, text="Export Date Range", command=self._export_range)
        export_range_button.pack(fill="x", pady=6)
        self.action_buttons.append(export_range_button)

        refresh_button = ttk.Button(actions, text="Refresh Dashboard", command=self.refresh_tables)
        refresh_button.pack(fill="x", pady=6)
        self.action_buttons.append(refresh_button)

        logout_button = ttk.Button(actions, text="Logout", command=self._build_login)
        logout_button.pack(fill="x", pady=6)
        self.action_buttons.append(logout_button)

        summary = ttk.Frame(container)
        summary.grid(row=1, column=1, sticky="nsew")
        summary.columnconfigure(0, weight=1)
        summary.columnconfigure(1, weight=1)
        summary.rowconfigure(1, weight=1)

        ttk.Label(summary, text="Registered People", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(summary, text="Recent Attendance", font=("Segoe UI", 12, "bold")).grid(row=0, column=1, sticky="w", pady=(0, 8))

        self.people_list = tk.Listbox(summary, height=20)
        self.people_list.grid(row=1, column=0, sticky="nsew", padx=(0, 8))

        self.attendance_table = ttk.Treeview(
            summary,
            columns=("name", "date", "time", "status", "confidence", "source"),
            show="headings",
            height=20,
        )
        for heading, width in (
            ("name", 120),
            ("date", 90),
            ("time", 90),
            ("status", 90),
            ("confidence", 90),
            ("source", 90),
        ):
            self.attendance_table.heading(heading, text=heading.title())
            self.attendance_table.column(heading, width=width, anchor="center")
        self.attendance_table.grid(row=1, column=1, sticky="nsew")

        self.refresh_tables()

    def refresh_tables(self) -> None:
        self.people_list.delete(0, tk.END)
        for person in list_people():
            self.people_list.insert(tk.END, person["name"])

        for item in self.attendance_table.get_children():
            self.attendance_table.delete(item)
        for row in list_attendance(limit=100):
            confidence = "" if row["confidence"] is None else f"{row['confidence']:.3f}"
            values = (
                row["name"],
                row["attendance_date"],
                row["attendance_time"],
                row["status"],
                confidence,
                row["source"],
            )
            self.attendance_table.insert("", tk.END, values=values)

        self.status_var.set("Dashboard refreshed")

    def _enroll_from_dialog(self) -> None:
        name = simpledialog.askstring("Enroll Person", "Enter person name:", parent=self.root)
        if not name:
            return

        image_count = simpledialog.askinteger(
            "Enroll Person",
            "How many images should be captured?",
            parent=self.root,
            minvalue=1,
            initialvalue=10,
        )
        if not image_count:
            return

        self._run_async(
            lambda: enroll_person(name=name, max_images=image_count),
            f"Enrolling {name}...",
            lambda _result: messagebox.showinfo("Enrollment Complete", f"Saved images for {name}."),
        )

    def _export_today(self) -> None:
        self._run_async(export_today, "Exporting today's attendance...", self._show_export_result)

    def _delete_selected_person(self) -> None:
        selection = self.people_list.curselection()
        if not selection:
            messagebox.showinfo("Select Person", "Please select a person from the Registered People list.")
            return

        name = self.people_list.get(selection[0])
        confirmed = messagebox.askyesno(
            "Delete Person",
            f"Delete {name} and remove their saved face data and attendance records?",
        )
        if not confirmed:
            return

        self._run_async(
            lambda: delete_person(name),
            f"Deleting {name}...",
            lambda _result, deleted_name=name: messagebox.showinfo("Deleted", f"{deleted_name} was removed successfully."),
        )

    def _export_range(self) -> None:
        default_day = date.today().isoformat()
        start_date = simpledialog.askstring("Export Range", "Start date (YYYY-MM-DD):", initialvalue=default_day, parent=self.root)
        if not start_date:
            return
        end_date = simpledialog.askstring("Export Range", "End date (YYYY-MM-DD):", initialvalue=default_day, parent=self.root)
        if not end_date:
            return
        self._run_async(
            lambda: export_attendance_to_excel(start_date, end_date),
            f"Exporting attendance from {start_date} to {end_date}...",
            self._show_export_result,
        )

    def _show_export_result(self, file_path) -> None:
        messagebox.showinfo("Export Complete", f"Excel file created:\n{file_path}")

    def _show_training_result(self, _result) -> None:
        messagebox.showinfo("Training Complete", "Model training finished successfully.")

    def _run_async(self, func, status_message: str, on_success=None) -> None:
        if self.is_task_running:
            messagebox.showinfo("Task Running", "Please finish the current action before starting another one.")
            return

        self.is_task_running = True
        self._set_action_state("disabled")
        self.status_var.set(status_message)

        def worker() -> None:
            try:
                result = func()
                self.root.after(0, lambda result=result, on_success=on_success: self._on_task_success(result, on_success))
            except Exception as exc:
                self.root.after(0, lambda exc=exc: self._on_task_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_task_success(self, result, on_success) -> None:
        self.is_task_running = False
        self._set_action_state("normal")
        self.status_var.set("Ready")
        self.refresh_tables()
        if on_success is not None:
            on_success(result)

    def _on_task_error(self, exc: Exception) -> None:
        self.is_task_running = False
        self._set_action_state("normal")
        self.status_var.set("Error")
        messagebox.showerror("Operation Failed", str(exc))

    def _set_action_state(self, state: str) -> None:
        for button in self.action_buttons:
            button.configure(state=state)

    def run(self) -> None:
        self.root.mainloop()


def launch_app() -> None:
    app = AttendanceApp()
    app.run()
