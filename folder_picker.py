"""
folder_picker.py
-----------------
Standalone helper that opens the native OS folder-browser dialog and prints
the chosen path to stdout.

Why this is a separate script instead of code inside app.py:
Tkinter requires its event loop (Tk()) to run on the MAIN thread of the
process that owns the display connection. Streamlit runs your script inside
its own worker thread (the ScriptRunner thread), not the process's main
thread. Calling tk.Tk() directly from app.py therefore violates Tk's
threading rules — depending on OS/platform this can hang indefinitely or
crash the whole Streamlit server, which is exactly the "app stops working"
symptom.

Running this file as a separate subprocess sidesteps the problem completely:
the subprocess gets a fresh process with its own real main thread, tkinter
is safe to use there, and app.py just waits for it to exit and reads the
chosen path from stdout. If anything goes wrong, only this subprocess is
affected — the Streamlit server keeps running.

Usage:
    python folder_picker.py [start_dir]

Prints the selected folder path to stdout (and nothing else) on success.
Prints nothing and exits with code 1 if the user cancels or an error occurs.
"""

import sys


def main():
    start_dir = sys.argv[1] if len(sys.argv) > 1 else ""

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        print(f"ERROR: tkinter unavailable: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # bring the dialog to the front
        root.update()

        selected = filedialog.askdirectory(
            initialdir=start_dir or None,
            title="Select download folder",
            parent=root,
        )

        root.destroy()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not selected:
        # User cancelled the dialog.
        sys.exit(1)

    # Print ONLY the path, so the parent process can read it cleanly from stdout.
    print(selected)
    sys.exit(0)


if __name__ == "__main__":
    main()