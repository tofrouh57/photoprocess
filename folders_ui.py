#!/usr/bin/env python3

from datetime import date
import tkinter as tk
from tkinter import messagebox, ttk

import psycopg

from main import database_settings, ensure_schema


def parse_date(value, label):
    value = value.strip()
    if not value:
        return None

    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc


def build_folder_query(
    path_filter,
    status_filter,
    discovered_from,
    discovered_to,
    processed_from,
    processed_to,
):
    conditions = []
    params = []

    path_filter = path_filter.strip()
    if path_filter:
        pattern = (
            path_filter
            if "%" in path_filter or "_" in path_filter
            else f"%{path_filter}%"
        )
        conditions.append("path ILIKE %s")
        params.append(pattern)

    if status_filter == "Pending":
        conditions.append("status = 'pending'")
    elif status_filter == "Processed":
        conditions.append("status = 'processed'")

    for column, start, end in (
        ("discovered_date", discovered_from, discovered_to),
        ("processed_date", processed_from, processed_to),
    ):
        if start is not None:
            conditions.append(f"{column} >= %s::date")
            params.append(start)
        if end is not None:
            conditions.append(f"{column} < (%s::date + INTERVAL '1 day')")
            params.append(end)

    query = """
        SELECT id, path, discovered_date, status, processed_date
        FROM folders
    """
    if conditions:
        query += "\nWHERE " + "\n  AND ".join(conditions)
    query += "\nORDER BY path"
    return query, params


class FolderBrowser:
    def __init__(self, root):
        self.root = root
        self.root.title("Photo Process - Folders")
        self.root.geometry("1100x650")
        self.root.minsize(700, 400)

        self.path_filter = tk.StringVar()
        self.status_filter = tk.StringVar(value="All")
        self.discovered_from = tk.StringVar()
        self.discovered_to = tk.StringVar()
        self.processed_from = tk.StringVar()
        self.processed_to = tk.StringVar()
        self.selection_status = tk.StringVar(value="0 folders selected")

        self._build_filters()
        self._build_folder_list()
        self._build_actions()
        self.refresh_folders()

    def _build_filters(self):
        frame = ttk.LabelFrame(self.root, text="Filters")
        frame.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(frame, text="Folder path").grid(
            row=0, column=0, sticky="w", padx=5, pady=4
        )
        path_entry = ttk.Entry(frame, textvariable=self.path_filter, width=36)
        path_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        path_entry.bind("<Return>", lambda _event: self.refresh_folders())

        ttk.Label(frame, text="Status").grid(
            row=0, column=2, sticky="w", padx=5, pady=4
        )
        status_box = ttk.Combobox(
            frame,
            textvariable=self.status_filter,
            values=("All", "Pending", "Processed"),
            state="readonly",
            width=12,
        )
        status_box.grid(row=0, column=3, sticky="w", padx=5, pady=4)
        status_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_folders())

        date_fields = (
            ("Discovered from", self.discovered_from),
            ("Discovered to", self.discovered_to),
            ("Processed from", self.processed_from),
            ("Processed to", self.processed_to),
        )
        for index, (label, variable) in enumerate(date_fields):
            row = 1 + index // 2
            column = (index % 2) * 2
            ttk.Label(frame, text=f"{label} (YYYY-MM-DD)").grid(
                row=row, column=column, sticky="w", padx=5, pady=4
            )
            ttk.Entry(frame, textvariable=variable, width=18).grid(
                row=row, column=column + 1, sticky="w", padx=5, pady=4
            )

        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=4, rowspan=3, sticky="ns", padx=8, pady=4)
        ttk.Button(buttons, text="Apply filters", command=self.refresh_folders).pack(
            fill="x", pady=2
        )
        ttk.Button(buttons, text="Clear filters", command=self.clear_filters).pack(
            fill="x", pady=2
        )
        frame.columnconfigure(1, weight=1)

    def _build_folder_list(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = (
            "id",
            "path",
            "discovered_date",
            "status",
            "processed_date",
        )
        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        headings = {
            "id": ("ID", 75),
            "path": ("Folder path", 500),
            "discovered_date": ("Discovered", 170),
            "status": ("Status", 100),
            "processed_date": ("Processed", 170),
        }
        for column, (heading, width) in headings.items():
            self.tree.heading(column, text=heading)
            self.tree.column(
                column,
                width=width,
                minwidth=60,
                stretch=column == "path",
                anchor="w" if column == "path" else "center",
            )

        vertical_scroll = ttk.Scrollbar(
            frame, orient="vertical", command=self.tree.yview
        )
        horizontal_scroll = ttk.Scrollbar(
            frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=vertical_scroll.set,
            xscrollcommand=horizontal_scroll.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical_scroll.grid(row=0, column=1, sticky="ns")
        horizontal_scroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

    def _build_actions(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=(5, 10))
        ttk.Label(frame, textvariable=self.selection_status).pack(side="left")
        ttk.Button(
            frame,
            text="Reset selected",
            command=self.reset_selected,
        ).pack(side="right", padx=(5, 0))
        ttk.Button(
            frame,
            text="Delete selected",
            command=self.delete_selected,
        ).pack(side="right")

    def _selection_changed(self, _event=None):
        count = len(self.tree.selection())
        noun = "folder" if count == 1 else "folders"
        self.selection_status.set(f"{count} {noun} selected")

    def _selected_ids(self):
        return [int(item) for item in self.tree.selection()]

    def _read_date_filters(self):
        values = (
            parse_date(self.discovered_from.get(), "Discovered from"),
            parse_date(self.discovered_to.get(), "Discovered to"),
            parse_date(self.processed_from.get(), "Processed from"),
            parse_date(self.processed_to.get(), "Processed to"),
        )
        labels = ("discovery", "processed")
        for index, label in enumerate(labels):
            start, end = values[index * 2:index * 2 + 2]
            if start and end and start > end:
                raise ValueError(
                    f"The {label} start date must not be after its end date."
                )
        return values

    def refresh_folders(self):
        try:
            dates = self._read_date_filters()
            query, params = build_folder_query(
                self.path_filter.get(),
                self.status_filter.get(),
                *dates,
            )
            with psycopg.connect(**database_settings()) as conn:
                ensure_schema(conn)
                with conn.cursor() as cursor:
                    cursor.execute(query, params)
                    rows = cursor.fetchall()
        except (psycopg.Error, RuntimeError, ValueError) as exc:
            messagebox.showerror("Could not load folders", str(exc), parent=self.root)
            return

        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for folder_id, path, discovered_date, status, processed_date in rows:
            self.tree.insert(
                "",
                "end",
                iid=str(folder_id),
                values=(
                    folder_id,
                    path,
                    discovered_date or "",
                    status or "pending",
                    processed_date or "",
                ),
            )
        self._selection_changed()

    def clear_filters(self):
        self.path_filter.set("")
        self.status_filter.set("All")
        self.discovered_from.set("")
        self.discovered_to.set("")
        self.processed_from.set("")
        self.processed_to.set("")
        self.refresh_folders()

    def reset_selected(self):
        folder_ids = self._selected_ids()
        if not folder_ids:
            messagebox.showinfo(
                "No folders selected",
                "Select one or more folder rows first.",
                parent=self.root,
            )
            return

        noun = "folder" if len(folder_ids) == 1 else "folders"
        if not messagebox.askyesno(
            "Reset selected folders",
            f"Reset processing status for {len(folder_ids)} {noun}?\n\n"
            "Existing metadata will be kept until a successful export replaces it.",
            parent=self.root,
        ):
            return

        try:
            with psycopg.connect(**database_settings()) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE folders
                        SET status = 'pending', processed_date = NULL
                        WHERE id = ANY(%s)
                        """,
                        (folder_ids,),
                    )
        except (psycopg.Error, RuntimeError) as exc:
            messagebox.showerror("Could not reset folders", str(exc), parent=self.root)
            return

        self.refresh_folders()

    def delete_selected(self):
        folder_ids = self._selected_ids()
        if not folder_ids:
            messagebox.showinfo(
                "No folders selected",
                "Select one or more folder rows first.",
                parent=self.root,
            )
            return

        noun = "folder" if len(folder_ids) == 1 else "folders"
        if not messagebox.askyesno(
            "Delete selected folders",
            f"Permanently delete {len(folder_ids)} {noun}?\n\n"
            "Their metadata and processing errors will also be deleted. "
            "This cannot be undone.",
            icon="warning",
            parent=self.root,
        ):
            return

        try:
            with psycopg.connect(**database_settings()) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM processing_errors
                        WHERE folder_id = ANY(%s)
                        """,
                        (folder_ids,),
                    )
                    cursor.execute(
                        """
                        DELETE FROM file_metadata
                        WHERE folder_id = ANY(%s)
                        """,
                        (folder_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM folders WHERE id = ANY(%s)",
                        (folder_ids,),
                    )
        except (psycopg.Error, RuntimeError) as exc:
            messagebox.showerror("Could not delete folders", str(exc), parent=self.root)
            return

        self.refresh_folders()


def main():
    root = tk.Tk()
    FolderBrowser(root)
    root.mainloop()


if __name__ == "__main__":
    main()
