#!/usr/bin/env python3
# Local replay desktop viewer. No network listener.
import json
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path


class RadarDesktop:
    def __init__(self, root):
        root.title("Reader Radar - local replay")
        root.geometry("1100x820")
        self.live_file = None
        self.frames = []
        self.cursor = 0
        self.image = None
        self.hidden = {}
        self.cal_pix = {}   # "A"/"B" → (px, py)
        self.cal_game = {}  # "A"/"B" → (gx, gz)
        self._picking = None

        # Row 1: file controls + bounds
        bar = tk.Frame(root)
        bar.pack(fill="x")
        tk.Button(bar, text="Open replay", command=self.replay).pack(side="left")
        tk.Button(bar, text="Watch local snapshot", command=self.live).pack(side="left")
        tk.Button(bar, text="Open map PNG", command=self.map_load).pack(side="left")
        tk.Label(bar, text="  ").pack(side="left")
        self.bounds = []
        for name, value in zip(("Min X", "Max X", "Min Z", "Max Z"), (-30, 30, -30, 30)):
            tk.Label(bar, text=name).pack(side="left")
            entry = tk.Entry(bar, width=8)
            entry.insert(0, str(value))
            entry.pack(side="left")
            self.bounds.append(entry)

        # Row 2: calibration workflow
        cal = tk.Frame(root)
        cal.pack(fill="x")
        tk.Label(cal, text="Calibrate:").pack(side="left")
        tk.Button(cal, text="Pick A on map", command=lambda: self.cal_pick("A")).pack(side="left")
        tk.Button(cal, text="Mark A = here", command=lambda: self.cal_confirm("A")).pack(side="left")
        tk.Label(cal, text=" | ").pack(side="left")
        tk.Button(cal, text="Pick B on map", command=lambda: self.cal_pick("B")).pack(side="left")
        tk.Button(cal, text="Mark B = here", command=lambda: self.cal_confirm("B")).pack(side="left")
        tk.Label(cal, text=" | ").pack(side="left")
        tk.Button(cal, text="Save cal…", command=self.save_cal).pack(side="left")
        tk.Button(cal, text="Load cal…", command=self.load_cal).pack(side="left")
        self.cal_info = tk.Label(cal, text="Load map PNG → Pick A & B → walk to each spot → Mark A/B = here")
        self.cal_info.pack(side="left")

        self.status = tk.Label(root, text="Session offline")
        self.status.pack()
        self.filters = tk.Frame(root)
        self.filters.pack(fill="x")
        self.canvas = tk.Canvas(root, bg="#101214")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._canvas_click)
        self.root = root
        self.tick()

    # ── calibration ──────────────────────────────────────────────────────────

    def cal_pick(self, pt):
        if not self.image:
            messagebox.showinfo("No map", "Load a map PNG first.")
            return
        self._picking = pt
        self.cal_info.config(text=f"Click on the map to place reference {pt}…")

    def _canvas_click(self, event):
        if not self._picking:
            return
        pt, self._picking = self._picking, None
        self.cal_pix[pt] = (event.x, event.y)
        self.cal_info.config(
            text=f"{pt} pinned at pixel ({event.x}, {event.y}). "
                 f"Walk your character to that spot, then click 'Mark {pt} = here'.")

    def cal_confirm(self, pt):
        if not self.live_file:
            messagebox.showinfo("No feed", "Start the live snapshot feed first.")
            return
        try:
            snap = json.loads(self.live_file.read_text(encoding="utf-8"))
            pos = snap["player"]["position"]
            self.cal_game[pt] = (pos["x"], pos["z"])
            self.cal_info.config(
                text=f"{pt} = ({pos['x']:.2f}, {pos['z']:.2f})  |  {self._pending_msg()}")
            self._apply_calibration()
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror("Read error", str(e))

    def _pending_msg(self):
        need = []
        for k in ("A", "B"):
            if k not in self.cal_pix:
                need.append(f"pick {k} on map")
            elif k not in self.cal_game:
                need.append(f"mark {k} = here")
        return "  |  ".join(need) if need else "both points set — calibration applied"

    def _apply_calibration(self):
        if not ({"A", "B"} <= self.cal_pix.keys() and {"A", "B"} <= self.cal_game.keys()):
            return
        if not self.image:
            return

        px_a, py_a = self.cal_pix["A"]
        px_b, py_b = self.cal_pix["B"]
        gx_a, gz_a = self.cal_game["A"]
        gx_b, gz_b = self.cal_game["B"]
        w, h = self.image.width(), self.image.height()

        if px_a == px_b or py_a == py_b:
            messagebox.showerror("Calibration",
                                 "Pick two points that differ in both X and Y pixel coordinates.")
            return

        # X: pixel_x = (gx - lo) / (hi - lo) * w
        span_x = (gx_a - gx_b) * w / (px_a - px_b)
        lo = gx_a - px_a * span_x / w
        hi = lo + span_x

        # Z: pixel_y = h - (gz - bottom) / (top - bottom) * h  (Y is flipped)
        span_z = (gz_a - gz_b) * h / (py_b - py_a)
        bottom = gz_a - (h - py_a) * span_z / h
        top = bottom + span_z

        if span_x <= 0 or span_z <= 0:
            messagebox.showwarning("Calibration",
                                   "Axes appear inverted — try swapping A and B, or check map orientation.")
            return

        for entry, val in zip(self.bounds, (lo, hi, bottom, top)):
            entry.delete(0, tk.END)
            entry.insert(0, f"{val:.4f}")

        self.cal_info.config(
            text=f"Calibrated  X: {lo:.1f}→{hi:.1f}  Z: {bottom:.1f}→{top:.1f}")

    def save_cal(self):
        vals = [e.get() for e in self.bounds]
        keys = ("min_x", "max_x", "min_z", "max_z")
        name = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Calibration JSON", "*.json")],
            initialfile="skeld_calibration.json")
        if name:
            Path(name).write_text(
                json.dumps(dict(zip(keys, vals)), indent=2), encoding="utf-8")

    def load_cal(self):
        name = filedialog.askopenfilename(filetypes=[("Calibration JSON", "*.json")])
        if not name:
            return
        try:
            data = json.loads(Path(name).read_text(encoding="utf-8"))
            for entry, key in zip(self.bounds, ("min_x", "max_x", "min_z", "max_z")):
                entry.delete(0, tk.END)
                entry.insert(0, str(data[key]))
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror("Load calibration", str(e))

    # ── file operations ───────────────────────────────────────────────────────

    def live(self):
        name = filedialog.askopenfilename(filetypes=[("Snapshot JSON", "*.json")])
        if name:
            self.live_file = Path(name)
            self.frames = []

    def replay(self):
        name = filedialog.askopenfilename(filetypes=[("Replay JSON", "*.json")])
        if not name:
            return
        try:
            doc = json.loads(Path(name).read_text(encoding="utf-8"))
            frames = doc["frames"]
            if not frames:
                raise ValueError("No frames")
            categories = sorted({e["category"] for f in frames for e in f["entities"]})
            self.live_file = None
            self.frames = frames
            self.cursor = 0
            self.target = doc["targetId"]
            for w in self.filters.winfo_children():
                w.destroy()
            self.hidden = {}
            for c in categories:
                var = tk.BooleanVar(value=True)
                self.hidden[c] = var
                tk.Checkbutton(self.filters, text=c, variable=var).pack(side="left")
        except (ValueError, KeyError, TypeError, OSError) as e:
            messagebox.showerror("Replay error", str(e))

    def map_load(self):
        name = filedialog.askopenfilename(filetypes=[("PNG image", "*.png")])
        if name:
            try:
                self.image = tk.PhotoImage(file=name)
            except tk.TclError as e:
                messagebox.showerror("Map error", str(e))

    # ── render loop ───────────────────────────────────────────────────────────

    def tick(self):
        if self.live_file:
            try:
                if time.time() - self.live_file.stat().st_mtime > 5:
                    raise ValueError("Stale")
                snap = json.loads(self.live_file.read_text(encoding="utf-8"))
                self.frames = [{"player": snap["player"], "entities": snap["entities"]}]
                self.target = snap["targetId"]
            except (OSError, ValueError, KeyError, TypeError):
                self.frames = []
                self.status.config(text="Session offline")
        self.canvas.delete("all")
        if self.image:
            self.canvas.create_image(0, 0, anchor="nw", image=self.image)
        if self.frames:
            try:
                frame = self.frames[self.cursor % len(self.frames)]
                self.cursor += 1
                lo, hi, bottom, top = [float(e.get()) for e in self.bounds]
                if hi <= lo or top <= bottom:
                    raise ValueError()
                w = self.image.width() if self.image else self.canvas.winfo_width()
                h = self.image.height() if self.image else self.canvas.winfo_height()
                rows = ([{"category": "local-player",
                          "position": frame["player"]["position"]}]
                        + frame["entities"])
                for row in rows:
                    if row["category"] in self.hidden and not self.hidden[row["category"]].get():
                        continue
                    p = row["position"]
                    x = (p["x"] - lo) / (hi - lo) * w
                    y = h - (p["z"] - bottom) / (top - bottom) * h
                    color = "#73d18b" if row["category"] == "local-player" else "#e8b44f"
                    self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color)
                self.status.config(
                    text=f"{self.target} | local capture | frame {self.cursor}")
            except (ValueError, KeyError, TypeError):
                self.status.config(text="Check capture and calibration")
        self.root.after(100, self.tick)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ReaderRadar local viewer")
    ap.add_argument("--snapshot", metavar="PATH", help="Auto-load live snapshot JSON")
    ap.add_argument("--map",      metavar="PATH", help="Auto-load map PNG")
    ap.add_argument("--cal",      metavar="PATH", help="Auto-load calibration JSON")
    args, _ = ap.parse_known_args()

    root = tk.Tk()
    app  = RadarDesktop(root)

    if args.snapshot:
        p = Path(args.snapshot)
        if p.exists() or True:   # allow pre-creation path
            app.live_file = p

    if args.map:
        try:
            app.image = tk.PhotoImage(file=args.map)
        except tk.TclError as e:
            print(f"Map load failed: {e}")

    if args.cal:
        try:
            data = json.loads(Path(args.cal).read_text(encoding="utf-8"))
            for entry, key in zip(app.bounds, ("min_x", "max_x", "min_z", "max_z")):
                entry.delete(0, tk.END)
                entry.insert(0, str(data[key]))
        except Exception as e:
            print(f"Cal load failed: {e}")

    root.mainloop()
