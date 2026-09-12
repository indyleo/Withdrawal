#!/usr/bin/env python3

import ast
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

# ============================================================
# Configuration
# ============================================================

APP_ID = "org.vinegarhq.Sober"
INSTANCES_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    / "Withdrawal"
    / "Instances"
)
INSTANCES_LIST = INSTANCES_DIR / "Instances.list"

SOBER_CONFIG_DIR = Path.home() / ".var" / "app" / APP_ID / "config" / "sober"

SOBER_CONFIG_FILE = SOBER_CONFIG_DIR / "config.json"

# Linux filenames cannot contain these characters.
INVALID_NAME_CHARS = re.compile(r"[/\:\0]")

# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("MultiSober")

# ============================================================
# Exceptions
# ============================================================


class InstanceAlreadyExist(Exception):
    pass


class InvalidInstanceName(Exception):
    pass


# ============================================================
# MultiSober Manager
# ============================================================


class MultiSoberManager:
    def __init__(self):
        INSTANCES_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.processes = {}

    # --------------------------------------------------------
    # Instance names
    # --------------------------------------------------------

    def validate_instance_name(self, name):
        name = name.strip()

        if not name:
            raise InvalidInstanceName("Instance name cannot be empty.")

        if name in (".", ".."):
            raise InvalidInstanceName("That instance name is not allowed.")

        if INVALID_NAME_CHARS.search(name):
            raise InvalidInstanceName("Instance name contains invalid characters.")

        if len(name) > 100:
            raise InvalidInstanceName("Instance name is too long.")

        return name

    # --------------------------------------------------------
    # Instance list
    # --------------------------------------------------------

    def _read_registered_instances(self):
        """Read the raw registry file, without touching the filesystem."""
        if not INSTANCES_LIST.exists():
            return []

        try:
            content = INSTANCES_LIST.read_text(encoding="utf-8").strip()

            if not content:
                return []

            instances = ast.literal_eval(content)

            if not isinstance(instances, list):
                log.warning("Instances.list does not contain a list.")
                return []

            return [str(name) for name in instances if isinstance(name, str)]

        except Exception as e:
            log.error(f"Failed to read {INSTANCES_LIST}: {e}")
            return []

    def list_instances(self):
        """
        Return the list of instances, reconciled against what actually
        exists on disk:

        - Registered names whose directory no longer exists are dropped.
        - Directories that exist on disk but aren't registered are added.

        If reconciliation changes anything, the registry file is rewritten
        so future reads stay in sync.
        """
        registered = self._read_registered_instances()

        # Keep only registered names that still have a directory.
        valid = [name for name in registered if (INSTANCES_DIR / name).exists()]

        # Pick up any directories on disk that aren't registered yet.
        if INSTANCES_DIR.exists():
            on_disk = sorted(p.name for p in INSTANCES_DIR.iterdir() if p.is_dir())

            for name in on_disk:
                if name not in valid:
                    valid.append(name)

        if valid != registered:
            log.info("Reconciled Instances.list with the filesystem.")
            self.save_instances(valid)

        return valid

    def save_instances(self, instances):
        INSTANCES_LIST.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_file = INSTANCES_LIST.with_suffix(".tmp")

        temp_file.write_text(
            repr(instances),
            encoding="utf-8",
        )

        temp_file.replace(INSTANCES_LIST)

    # --------------------------------------------------------
    # Add
    # --------------------------------------------------------

    def add_instance(self, name):
        name = self.validate_instance_name(name)

        path = INSTANCES_DIR / name

        if path.exists():
            raise InstanceAlreadyExist(f"Instance '{name}' already exists.")

        path.mkdir(
            parents=True,
            exist_ok=False,
        )

        instances = self.list_instances()

        if name not in instances:
            instances.append(name)
            self.save_instances(instances)

        log.info(f"Created instance '{name}'")

    # --------------------------------------------------------
    # Delete
    # --------------------------------------------------------

    def delete_instance(self, name):
        path = INSTANCES_DIR / name

        if not path.exists():
            return

        if self.is_running(name):
            raise RuntimeError(f"Instance '{name}' is currently running.")

        shutil.rmtree(path)

        instances = self.list_instances()

        if name in instances:
            instances.remove(name)
            self.save_instances(instances)

        log.info(f"Deleted instance '{name}'")

    # --------------------------------------------------------
    # Rename
    # --------------------------------------------------------

    def rename_instance(self, old_name, new_name):
        new_name = self.validate_instance_name(new_name)

        if old_name == new_name:
            return

        old_path = INSTANCES_DIR / old_name
        new_path = INSTANCES_DIR / new_name

        if not old_path.exists():
            raise FileNotFoundError(f"Instance '{old_name}' does not exist.")

        if new_path.exists():
            raise InstanceAlreadyExist(f"Instance '{new_name}' already exists.")

        if self.is_running(old_name):
            raise RuntimeError(f"Instance '{old_name}' is currently running.")

        old_path.rename(new_path)

        instances = self.list_instances()

        if old_name in instances:
            index = instances.index(old_name)
            instances[index] = new_name
            self.save_instances(instances)

        log.info(f"Renamed instance '{old_name}' " f"to '{new_name}'")

    # --------------------------------------------------------
    # Running state
    # --------------------------------------------------------

    def is_running(self, name):
        process = self.processes.get(name)

        if process is None:
            return False

        if process.poll() is None:
            return True

        self.processes.pop(name, None)

        return False

    # --------------------------------------------------------
    # Launch
    # --------------------------------------------------------

    def run_instance(self, name):
        instance_dir = INSTANCES_DIR / name

        if not instance_dir.exists():
            raise FileNotFoundError(f"Instance '{name}' does not exist.")

        if self.is_running(name):
            raise RuntimeError(f"Instance '{name}' is already running.")

        env = os.environ.copy()

        # Give this profile its own HOME. This alone is enough to isolate
        # each instance: Flatpak sandboxes an app's persistent config/data/
        # cache under $HOME/.var/app/<APP_ID>/, so a distinct HOME per
        # instance gives each one its own Sober state.
        #
        # Deliberately NOT overriding XDG_DATA_HOME/XDG_CONFIG_HOME/
        # XDG_CACHE_HOME here: the `flatpak` command itself (running on the
        # host, before any sandboxing happens) uses XDG_DATA_HOME to find
        # where the user's Flatpak apps are installed. Redirecting it to a
        # per-instance folder makes `flatpak run` unable to find Sober at
        # all, and it fails silently since stderr is discarded.
        env["HOME"] = str(instance_dir)

        log.info(f"Launching instance '{name}'")

        try:
            process = subprocess.Popen(
                [
                    "flatpak",
                    "run",
                    APP_ID,
                ],
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )

            self.processes[name] = process

            log.info(f"Instance '{name}' launched " f"(PID {process.pid})")

            return process

        except FileNotFoundError:
            raise RuntimeError(
                "Flatpak was not found. " "Make sure Flatpak is installed."
            )

        except Exception as e:
            log.error(f"Failed to launch '{name}': {e}")
            raise

    # --------------------------------------------------------
    # Stop
    # --------------------------------------------------------

    def stop_instance(self, name):
        process = self.processes.get(name)

        if process is None:
            return False

        if process.poll() is not None:
            self.processes.pop(name, None)
            return False

        log.info(f"Stopping instance '{name}' " f"(PID {process.pid})")

        try:
            process.terminate()
            process.wait(timeout=5)

        except subprocess.TimeoutExpired:
            log.warning(f"Instance '{name}' did not terminate. " "Killing it.")

            process.kill()
            process.wait()

        finally:
            self.processes.pop(name, None)

        return True


# ============================================================
# Instance Row
# ============================================================


class InstanceRow(Gtk.ListBoxRow):
    def __init__(
        self,
        name,
        manager,
        refresh_cb,
        run_cb,
    ):
        super().__init__()

        self.name = name
        self.manager = manager
        self.refresh_cb = refresh_cb
        self.run_cb = run_cb

        self.editing = False

        self.set_child(self._build_row())

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def _build_row(self):
        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )

        self.label = Gtk.Label(
            label=self.name,
            xalign=0,
        )

        self.label.set_hexpand(True)

        self.import_button = Gtk.Button(label="Import Settings")

        self.edit_button = Gtk.Button(label="Edit")

        self.delete_button = Gtk.Button(label="Delete")

        self.delete_button.add_css_class("destructive-action")

        self.run_button = Gtk.Button(label="Run")

        self.run_button.add_css_class("suggested-action")

        self.import_button.connect(
            "clicked",
            self.on_import_clicked,
        )

        self.edit_button.connect(
            "clicked",
            self.on_edit_clicked,
        )

        self.delete_button.connect(
            "clicked",
            self.on_delete_clicked,
        )

        self.run_button.connect(
            "clicked",
            self.on_run_clicked,
        )

        self.main_box.append(self.label)

        self.main_box.append(self.import_button)

        self.main_box.append(self.edit_button)

        self.main_box.append(self.delete_button)

        self.main_box.append(self.run_button)

        return self.main_box

    # --------------------------------------------------------
    # Import
    # --------------------------------------------------------

    def on_import_clicked(self, button):
        source_file = SOBER_CONFIG_FILE

        # Since run_instance() only overrides HOME for this instance,
        # Flatpak stores this app's config under
        # <instance_dir>/.var/app/<APP_ID>/config/sober/ - matching that
        # is what makes the imported settings actually get picked up.
        destination_dir = (
            INSTANCES_DIR / self.name / ".var" / "app" / APP_ID / "config" / "sober"
        )

        destination_file = destination_dir / "config.json"

        if not source_file.exists():
            self.show_error(
                "Sober's configuration file was not found:\n\n" f"{source_file}"
            )
            return

        try:
            destination_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            shutil.copy2(
                source_file,
                destination_file,
            )

            self.show_message(
                "Import complete",
                f"Settings imported for '{self.name}'.",
            )

            log.info(f"Imported settings for '{self.name}'")

        except Exception as e:
            log.error(f"Import failed for '{self.name}': {e}")

            self.show_error(f"Failed to import settings:\n\n{e}")

    # --------------------------------------------------------
    # Edit
    # --------------------------------------------------------

    def on_edit_clicked(self, button):
        if self.editing:
            return

        self.editing = True

        self.entry = Gtk.Entry()
        self.entry.set_text(self.name)
        self.entry.set_hexpand(True)

        self.save_button = Gtk.Button(label="Save")

        self.save_button.add_css_class("suggested-action")

        self.cancel_button = Gtk.Button(label="Cancel")

        self.main_box.remove(self.label)

        self.main_box.remove(self.import_button)

        self.main_box.remove(self.edit_button)

        self.main_box.remove(self.delete_button)

        self.main_box.remove(self.run_button)

        self.main_box.append(self.entry)

        self.main_box.append(self.save_button)

        self.main_box.append(self.cancel_button)

        self.save_button.connect(
            "clicked",
            self.on_save_clicked,
        )

        self.cancel_button.connect(
            "clicked",
            self.on_cancel_clicked,
        )

        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    # --------------------------------------------------------
    # Save rename
    # --------------------------------------------------------

    def on_save_clicked(self, button):
        new_name = self.entry.get_text().strip()

        if not new_name:
            self.show_error("Name cannot be empty.")
            return

        try:
            self.manager.rename_instance(
                self.name,
                new_name,
            )

            self.name = new_name

            self.editing = False

            self.refresh_cb()

        except (
            InstanceAlreadyExist,
            InvalidInstanceName,
            FileNotFoundError,
            RuntimeError,
        ) as e:
            self.show_error(str(e))

    # --------------------------------------------------------
    # Cancel rename
    # --------------------------------------------------------

    def on_cancel_clicked(self, button):
        self.editing = False
        self.refresh_cb()

    # --------------------------------------------------------
    # Delete
    # --------------------------------------------------------

    def on_delete_clicked(self, button):
        dialog = Adw.AlertDialog(
            heading="Delete instance?",
            body=(f"Delete instance '{self.name}'?\n\n" "This cannot be undone."),
        )

        dialog.add_response(
            "cancel",
            "Cancel",
        )

        dialog.add_response(
            "delete",
            "Delete",
        )

        dialog.set_response_appearance(
            "delete",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )

        dialog.connect(
            "response",
            self._handle_delete_response,
        )

        dialog.present(self.get_root())

    def _handle_delete_response(
        self,
        dialog,
        response,
    ):
        if response != "delete":
            return

        try:
            self.manager.delete_instance(self.name)

            self.refresh_cb()

        except Exception as e:
            self.show_error(str(e))

    # --------------------------------------------------------
    # Run
    # --------------------------------------------------------

    def on_run_clicked(self, button):
        self.run_cb(self.name)

    # --------------------------------------------------------
    # Message
    # --------------------------------------------------------

    def show_message(
        self,
        heading,
        body,
    ):
        dialog = Adw.AlertDialog(
            heading=heading,
            body=body,
        )

        dialog.add_response(
            "ok",
            "OK",
        )

        dialog.present(self.get_root())

    def show_error(self, message):
        self.show_message(
            "Error",
            message,
        )


# ============================================================
# Main Window
# ============================================================


class MultiSoberWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(
            application=app,
            title="MultiSober Manager",
        )

        self.set_default_size(
            800,
            500,
        )

        self.manager = MultiSoberManager()

        # ----------------------------------------------------
        # Toolbar
        # ----------------------------------------------------

        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()

        self.style_manager = Adw.StyleManager.get_default()

        self.dark_mode_button = Gtk.ToggleButton()

        self.dark_mode_button.set_icon_name("weather-clear-night-symbolic")

        self.dark_mode_button.set_tooltip_text("Toggle dark mode")

        self.dark_mode_button.set_active(self.style_manager.get_dark())

        self.dark_mode_button.connect(
            "toggled",
            self.on_dark_mode_toggled,
        )

        header.pack_end(self.dark_mode_button)

        toolbar_view.add_top_bar(header)

        self.set_content(toolbar_view)

        # ----------------------------------------------------
        # Main content
        # ----------------------------------------------------

        self.vbox = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )

        toolbar_view.set_content(self.vbox)

        # ----------------------------------------------------
        # Instance list
        # ----------------------------------------------------

        self.scrolled_window = Gtk.ScrolledWindow()

        self.scrolled_window.set_hexpand(True)

        self.scrolled_window.set_vexpand(True)

        self.list_box = Gtk.ListBox()

        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)

        self.list_box.add_css_class("boxed-list")

        self.scrolled_window.set_child(self.list_box)

        self.vbox.append(self.scrolled_window)

        # ----------------------------------------------------
        # Add button
        # ----------------------------------------------------

        self.add_button = Gtk.Button(label="Add Instance")

        self.add_button.add_css_class("suggested-action")

        self.add_button.connect(
            "clicked",
            self.on_add_instance_clicked,
        )

        self.vbox.append(self.add_button)

        self.refresh_instances()

    # --------------------------------------------------------
    # Dark mode
    # --------------------------------------------------------

    def on_dark_mode_toggled(
        self,
        button,
    ):
        if button.get_active():
            self.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            self.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

    # --------------------------------------------------------
    # Refresh
    # --------------------------------------------------------

    def refresh_instances(self):
        children = []

        child = self.list_box.get_first_child()

        while child is not None:
            children.append(child)
            child = child.get_next_sibling()

        for child in children:
            self.list_box.remove(child)

        instances = self.manager.list_instances()

        for name in instances:
            row = InstanceRow(
                name,
                self.manager,
                self.refresh_instances,
                self.run_instance,
            )

            self.list_box.append(row)

    # --------------------------------------------------------
    # Add instance
    # --------------------------------------------------------

    def on_add_instance_clicked(
        self,
        button,
    ):
        dialog = Adw.AlertDialog(
            heading="Add Instance",
        )

        entry = Gtk.Entry()

        entry.set_placeholder_text("Enter instance name")

        entry.set_hexpand(True)

        dialog.set_extra_child(entry)

        dialog.add_response(
            "cancel",
            "Cancel",
        )

        dialog.add_response(
            "add",
            "Add",
        )

        dialog.set_response_appearance(
            "add",
            Adw.ResponseAppearance.SUGGESTED,
        )

        dialog.set_default_response("add")

        entry.connect(
            "activate",
            lambda e: dialog.emit(
                "response",
                "add",
            ),
        )

        def handle_response(
            d,
            response,
        ):
            if response != "add":
                return

            name = entry.get_text().strip()

            try:
                self.manager.add_instance(name)

                self.refresh_instances()

            except (
                InstanceAlreadyExist,
                InvalidInstanceName,
            ) as e:
                self.show_error(str(e))

        dialog.connect(
            "response",
            handle_response,
        )

        dialog.present(self)

    # --------------------------------------------------------
    # Run instance
    # --------------------------------------------------------

    def run_instance(self, name):
        try:
            self.manager.run_instance(name)

        except Exception as e:
            log.error(f"Unable to launch '{name}': {e}")

            self.show_error(f"Unable to launch '{name}':\n\n{e}")

    # --------------------------------------------------------
    # Dialogs
    # --------------------------------------------------------

    def show_message(
        self,
        heading,
        body,
    ):
        dialog = Adw.AlertDialog(
            heading=heading,
            body=body,
        )

        dialog.add_response(
            "ok",
            "OK",
        )

        dialog.present(self)

    def show_error(self, message):
        self.show_message(
            "Error",
            message,
        )


# ============================================================
# Application
# ============================================================


class MultiSoberApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.example.MultiSober")

    def do_activate(self):
        win = MultiSoberWindow(self)

        win.present()


# ============================================================
# Main
# ============================================================


def main():
    app = MultiSoberApp()
    return app.run()


if __name__ == "__main__":
    main()
