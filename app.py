"""
Enterprise Focus Intelligence Dashboard
A modern productivity monitoring application with gamification and analytics.
"""

import logging
import time
import threading 
import ctypes
import sqlite3
import re
import joblib
import numpy as np
import pygetwindow as gw
import tkinter as tk

from tkinter import ttk, messagebox
from datetime import datetime, timedelta
from pynput import mouse, keyboard
from collections import deque
from typing import Dict, List

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
from comtypes import CoInitialize, CoUninitialize

# Configure logging
logging.basicConfig(
    filename="focus_app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# DPI Awareness
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except:
    try: 
        ctypes.windll.user32.SetProcessDPIAware()
    except: 
        pass

# ============================================================================
# BACKEND ENGINE
# ============================================================================

class Engine:
    """Backend monitoring engine"""
    def __init__(self):
        self.db_name = "daily_logs.db"
        self.is_running = True
        self.paused = False
        self.data_lock = threading.Lock()
        
        self.key_rates_history = []
        self.mouse_rates_history = []
        self.calib_keys = 35.0
        self.calib_mouse = 500.0
        self.alpha = 0.45 
        
        self.key_count = 0
        self.mouse_count = 0
        self.smooth_keys = 0.0
        self.smooth_mouse = 0.0
        
        self.battery = 100.0
        self.current_interval = 5.0 

        # UI-friendly state cache
        self.last_state = "idle"
        self.last_app = "Unknown"
        self.last_battery = self.battery
        self.last_time = datetime.now()
        self.session_start = datetime.now()

        self.init_db()
        self.recover_battery_state()
        
        # Load Random Forest model
        try:
            self.model = joblib.load("focus_rf_model.pkl")
            logging.info("Successfully loaded Random Forest model.")
        except Exception as e:
            logging.error(f"Could not load RF model: {e}")
            self.model = None
            
        # Base ML predictions map to the 4 core states
        self.state_map = {0: "idle", 1: "lowfocus", 2: "focus", 3: "not attentive"}
        self.app_switch_timestamps = deque()
        
        # Drift detection: track last 5 seconds of activity
        self.activity_buffer = deque(maxlen=1)  # Stores (keys, mouse) from last interval
        self.drift_detected = False

    def init_db(self):
        """Initialize database with proper connection handling"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_name, timeout=10.0, isolation_level='DEFERRED')
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")  # Better performance
            conn.execute("PRAGMA busy_timeout=10000;")  # 10 second timeout
            conn.execute('''CREATE TABLE IF NOT EXISTS logs 
                            (time_full TEXT UNIQUE, state TEXT, active_app TEXT, interval REAL, battery REAL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS reports 
                            (report_date TEXT, period TEXT, report_json TEXT)''')
            conn.commit()
        except Exception as e:
            logging.error(f"Could not initialize database: {e}")
        finally:
            if conn:
                conn.close()

    def recover_battery_state(self):
        """Recover battery state from last session with proper connection handling"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_name, timeout=10.0, isolation_level='DEFERRED')
            cursor = conn.execute("SELECT time_full, battery FROM logs ORDER BY time_full DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                try:
                    last_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S.%f")
                except ValueError:
                    last_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                gap = (datetime.now() - last_time).total_seconds() / 60
                self.battery = 100.0 if gap >= 30 else min(100.0, row[1] + (gap * 2.0))
        except Exception as e: 
            logging.error(f"Failed to load old battery: {e}")
            self.battery = 100.0
        finally:
            if conn:
                conn.close()

    def get_active_app(self):
        try:
            win = gw.getActiveWindow()
            if not win or not win.title: return "Desktop"
            # Cache the title to avoid repeated processing
            title = win.title.strip()
            parts = re.split(r'[-|—–:]', title)
            return parts[-1].strip() if len(parts) > 1 else title
        except: 
            return "System"

    def is_audio_playing(self):
        # Cache audio check to avoid expensive COM calls every cycle
        if not hasattr(self, '_last_audio_check'):
            self._last_audio_check = 0
            self._last_audio_state = False
        
        current_time = time.time()
        # Only check audio every 2 seconds instead of every cycle
        if current_time - self._last_audio_check < 2.0:
            return self._last_audio_state
        
        self._last_audio_check = current_time
        try:
            for s in AudioUtilities.GetAllSessions():
                if s.Process and s.State == 1:
                    if s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue() > 0.001: 
                        self._last_audio_state = True
                        return True
            self._last_audio_state = False
            return False
        except: 
            self._last_audio_state = False
            return False

    def on_key(self, key):
        with self.data_lock: 
            self.key_count += 1
        
    def on_move(self, x, y):
        # Throttle mouse movement tracking to reduce overhead
        if not hasattr(self, '_last_mouse_time'):
            self._last_mouse_time = 0
        
        current_time = time.time()
        # Only count mouse movement every 0.1 seconds
        if current_time - self._last_mouse_time >= 0.1:
            with self.data_lock:
                self.mouse_count += 1
            self._last_mouse_time = current_time 

    def on_click(self, x, y, button, pressed):
        if pressed:
            with self.data_lock:
                self.mouse_count += 15 

    def analyze(self):
        CoInitialize() 
        try:
            while self.is_running:
                if self.paused:
                    time.sleep(0.1)
                    continue

                # --- NEW POLLING SLEEP: 1.0s intervals to reduce CPU usage ---
                sleep_chunks = int(self.current_interval / 1.0)
                for _ in range(sleep_chunks):
                    if not self.is_running or self.paused:
                        break
                    time.sleep(1.0)
                    
                    current_app = self.get_active_app()
                    if current_app != self.last_app and self.last_app != "Unknown":
                        self.app_switch_timestamps.append(time.time())
                        self.last_app = current_app
                    elif self.last_app == "Unknown":
                        self.last_app = current_app

                with self.data_lock:
                    raw_k, raw_m = self.key_count, self.mouse_count
                    self.key_count = self.mouse_count = 0
                
                if raw_k > 0: self.key_rates_history.append(raw_k)
                if raw_m > 0: self.mouse_rates_history.append(raw_m)
                self.key_rates_history = self.key_rates_history[-180:]
                self.mouse_rates_history = self.mouse_rates_history[-180:]

                # Optimize calibration - only recalculate every 10 cycles
                if not hasattr(self, '_calib_counter'):
                    self._calib_counter = 0
                self._calib_counter += 1
                
                if self._calib_counter >= 10 and len(self.key_rates_history) > 20:
                    sorted_keys = sorted(self.key_rates_history)
                    sorted_mouse = sorted(self.mouse_rates_history)
                    self.calib_keys = max(sorted_keys[int(len(sorted_keys)*0.95)], 20.0)
                    self.calib_mouse = max(sorted_mouse[int(len(sorted_mouse)*0.95)], 200.0)
                    self._calib_counter = 0
                    
                if raw_k == 0: 
                    self.smooth_keys = self.smooth_keys * 0.7
                else: 
                    self.smooth_keys = (self.alpha * raw_k) + ((1 - self.alpha) * self.smooth_keys)

                if raw_m == 0: 
                    self.smooth_mouse = self.smooth_mouse * 0.7
                else: 
                    self.smooth_mouse = (self.alpha * raw_m) + ((1 - self.alpha) * self.smooth_mouse)
                
                # Use raw values for better responsiveness
                nk = min(raw_k / self.calib_keys, 1.5)
                nm = min(raw_m / self.calib_mouse, 1.5)
                
                app = self.last_app
                current_time = time.time()

                # Cleanup older than 60 seconds
                while self.app_switch_timestamps and current_time - self.app_switch_timestamps[0] > 60:
                    self.app_switch_timestamps.popleft()

                # --- COGNITIVE DRIFT DETECTION ---
                # Detect pre-switch hesitation (zero activity in 5 seconds BEFORE app switch)
                app_just_switched = False
                if len(self.app_switch_timestamps) > 0:
                    time_since_last_switch = current_time - self.app_switch_timestamps[-1]
                    if time_since_last_switch <= self.current_interval:
                        app_just_switched = True
                
                if app_just_switched and len(self.activity_buffer) > 0:
                    prev_keys, prev_mouse = self.activity_buffer[-1] if len(self.activity_buffer) > 0 else (0, 0)
                    # Drift = zero keys AND mouse <20% of calibrated
                    if prev_keys == 0 and prev_mouse < (self.calib_mouse * 0.2):
                        self.drift_detected = True
                        logging.info(f"🌫️ Drift detected before switch to {app}")
                
                # Store current activity for next interval
                self.activity_buffer.append((raw_k, raw_m))

                # --- STATE INFERENCE WITH PRIORITY ---
                # Priority: Drift > ML Inference
                if self.drift_detected:
                    s = "drift"
                    self.drift_detected = False
                elif self.model is not None:
                    try:
                        recent_switches = len(self.app_switch_timestamps)
                        features = np.array([[nk, nm, recent_switches]])
                        prediction = self.model.predict(features)[0]
                        s = self.state_map.get(prediction, "idle")
                    except Exception as e:
                        logging.error(f"Inference error: {e}")
                        s = "idle"
                else:
                    s = "idle"

                # --- EXPLICIT SWITCHING OVERRIDE (4 times under 10 seconds) ---
                switches_in_last_10s = len([t for t in self.app_switch_timestamps if current_time - t <= 10])
                if switches_in_last_10s >= 4 and s != "drift":
                    s = "switching"

                # --- ENERGY CALCULATION ---
                if s == "idle":
                    if self.is_audio_playing():
                        s = "Media Consumption"
                        bc = -0.1
                    else:
                        bc = 0.5
                elif s == "drift":
                    bc = -0.3  # Light drain - pre-switch hesitation
                else:
                    bc = {"lowfocus": -0.5, "focus": -0.8, "not attentive": -1.4, "switching": -2.0}.get(s, -1.2)
                
                self.battery = max(0.0, min(100.0, self.battery + bc))
                now = datetime.now()

                self.last_time = now
                self.last_state = s
                self.last_app = app
                self.last_battery = round(self.battery, 1)

                # Batch database writes - only write every 3 cycles (15 seconds)
                if not hasattr(self, '_db_write_counter'):
                    self._db_write_counter = 0
                    self._db_write_queue = []
                
                # Add to queue
                now_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")
                self._db_write_queue.append((now_str, s, app, self.current_interval, round(self.battery, 1)))
                self._db_write_counter += 1
                
                # Write in batches every 3 cycles
                if self._db_write_counter >= 3:
                    conn = None
                    try:
                        conn = sqlite3.connect(self.db_name, timeout=10.0, isolation_level='DEFERRED')
                        conn.executemany(
                            "INSERT OR REPLACE INTO logs VALUES (?, ?, ?, ?, ?)",
                            self._db_write_queue
                        )
                        conn.commit()
                        self._db_write_queue = []
                        self._db_write_counter = 0
                    except Exception as e: 
                        logging.error(f"Database write error: {e}")
                        if conn:
                            conn.rollback()
                    finally:
                        if conn:
                            conn.close()

        finally: 
            CoUninitialize()

    def start(self):
        self.kb_listener = keyboard.Listener(on_press=self.on_key)
        self.mouse_listener = mouse.Listener(on_move=self.on_move, on_click=self.on_click)
        self.kb_listener.start()
        self.mouse_listener.start()
        threading.Thread(target=self.analyze, daemon=True).start()

    def stop(self):
        """Stop the engine and cleanup resources"""
        self.is_running = False
        try:
            self.kb_listener.stop()
            self.mouse_listener.stop()
        except Exception:
            pass
        
        try:
            conn = sqlite3.connect(self.db_name, timeout=10.0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.commit()
            conn.close()
            logging.info("Database connections closed successfully")
        except Exception as e:
            logging.error(f"Error closing database: {e}")

# ============================================================================
# UI COMPONENTS
# ============================================================================

class ScrollableFrame(tk.Frame):
    """A helper class to create scrollable frames in Tkinter"""
    def __init__(self, container, bg_color, *args, **kwargs):
        super().__init__(container, bg=bg_color, *args, **kwargs)
        self.canvas = tk.Canvas(self, bg=bg_color, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=bg_color)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")

        self.canvas.bind('<Configure>', self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)
        
    def _on_mousewheel(self, event):
        if self.canvas.winfo_ismapped():
            self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")


# ============================================================================
# ENTERPRISE UI APPLICATION
# ============================================================================

class EnterpriseProductivityDashboard:
    """Main enterprise dashboard application"""
    
    # Color scheme
    COLORS = {
        'bg_primary': '#0f172a',
        'bg_secondary': '#1e293b',
        'bg_tertiary': '#334155',
        'accent_blue': '#3b82f6',
        'accent_green': '#10b981',
        'accent_yellow': '#f59e0b',
        'accent_red': '#ef4444',
        'text_primary': '#f8fafc',
        'text_secondary': '#94a3b8',
        'text_tertiary': '#64748b',
        'success': '#10b981',
        'warning': '#f59e0b',
        'danger': '#ef4444',
        'info': '#3b82f6',
    }
    
    STATE_COLORS = {
        "idle": "#10b981",              # Emerald Green
        "Media Consumption": "#3b82f6", # Blue
        "lowfocus": "#f59e0b",          # Yellow
        "focus": "#8b5cf6",             # Purple
        "not attentive": "#ef4444",     # Red
        "switching": "#ec4899",         # Pink
        "drift": "#6b7280",             # Grey-Blue (Cognitive Drift)
    }
    
    def __init__(self, engine: Engine):
        self.engine = engine
        
        # Session statistics
        self.session_stats = {
            'session_count': 1,
            'total_keystrokes': 0,
            'total_mouse_moves': 0,
            'focus_streak_minutes': 0,
            'deep_focus_minutes': 0,
            'productivity_score': 0,
            'session_start_hour': datetime.now().hour,
            'session_minutes': 0,
            'idle_seconds': 0,
        }
        
        # State history tracking: 1 Hour Window (720 records at 5 second intervals)
        self.state_history = deque(maxlen=720) 
        
        # UI state
        self.current_view = "dashboard"
        self.notification_widgets = []
        self.history_labels = [] 
        
        self.animation_running = False
        
        # Initialize main window
        self.root = tk.Tk()
        self.root.title("Attention Flow Cartographer")
        self.root.geometry("1600x900")
        self.root.minsize(1400, 800)
        self.root.configure(bg=self.COLORS['bg_primary'])
        
        self._show_welcome_screen()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _show_welcome_screen(self):
        """Innovative Cartographer Animation - Title pops up immediately at center"""
        self.animation_running = True
        
        # Create full-screen canvas
        self.welcome_canvas = tk.Canvas(
            self.root,
            bg="#0a0e1a",
            highlightthickness=0
        )
        self.welcome_canvas.pack(fill="both", expand=True)
        
        # Force canvas to render and get dimensions
        self.root.update()
        w = self.welcome_canvas.winfo_width()
        h = self.welcome_canvas.winfo_height()
        self.cx = w // 2
        self.cy = h // 2
        
        # Start animation immediately
        self._phase1_title_explosion()
    
    def _phase1_title_explosion(self):
        """Title explodes from center"""
        import math
        import random
        
        # Create particles
        self.particles = []
        for i in range(40):
            angle = (i / 40) * 360
            particle = self.welcome_canvas.create_oval(
                self.cx - 3, self.cy - 3,
                self.cx + 3, self.cy + 3,
                fill="#00d9ff",
                outline=""
            )
            self.particles.append({'id': particle, 'angle': angle, 'speed': random.uniform(2, 4)})
        
        # Create title (starts invisible)
        self.title = self.welcome_canvas.create_text(
            self.cx, self.cy - 20,
            text="ATTENTION FLOW\nCARTOGRAPHER",
            font=("Consolas", 1, "bold"),
            fill="#00d9ff",
            anchor="center",
            justify="center"
        )
        
        # Animate explosion
        self._animate_explosion(0)
    
    def _animate_explosion(self, frame):
        """Animate particles exploding outward and title growing"""
        if frame > 30:
            # Clean up particles
            for p in self.particles:
                self.welcome_canvas.delete(p['id'])
            # Start grid
            self.root.after(200, self._phase2_grid)
            return
        
        try:
            import math
            
            # Move particles outward
            for p in self.particles:
                rad = math.radians(p['angle'])
                dist = frame * p['speed'] * 3
                x = self.cx + dist * math.cos(rad)
                y = self.cy + dist * math.sin(rad)
                self.welcome_canvas.coords(p['id'], x-3, y-3, x+3, y+3)
                
                # Fade particles
                if frame > 20:
                    alpha = int(255 * (1 - (frame - 20) / 10))
                    try:
                        color = f"#{alpha:02x}{alpha:02x}ff"
                        self.welcome_canvas.itemconfig(p['id'], fill=color)
                    except:
                        pass
            
            # Grow title
            size = min(48, int(frame * 1.6))
            self.welcome_canvas.itemconfig(self.title, font=("Consolas", size, "bold"))
            
            self.root.after(25, lambda: self._animate_explosion(frame + 1))
        except:
            pass
    
    def _phase2_grid(self):
        """Animated grid forms from center"""
        self.grid_lines = []
        spacing = 60
        
        # Draw grid from center outward
        for i in range(15):
            self.root.after(i * 40, lambda i=i: self._draw_grid_ring(i, spacing))
        
        # Start radar
        self.root.after(800, self._phase3_radar)
    
    def _draw_grid_ring(self, ring, spacing):
        """Draw one ring of grid"""
        try:
            dist = ring * spacing
            
            # Vertical lines
            for x in [self.cx - dist, self.cx + dist]:
                if 0 <= x <= self.welcome_canvas.winfo_width():
                    line = self.welcome_canvas.create_line(
                        x, 0, x, self.welcome_canvas.winfo_height(),
                        fill="#1e40af",
                        width=1,
                        dash=(4, 4)
                    )
                    self.grid_lines.append(line)
            
            # Horizontal lines
            for y in [self.cy - dist, self.cy + dist]:
                if 0 <= y <= self.welcome_canvas.winfo_height():
                    line = self.welcome_canvas.create_line(
                        0, y, self.welcome_canvas.winfo_width(), y,
                        fill="#1e40af",
                        width=1,
                        dash=(4, 4)
                    )
                    self.grid_lines.append(line)
        except:
            pass
    
    def _phase3_radar(self):
        """Radar sweep with data points"""
        import math
        import random
        
        # Create radar line
        self.radar_line = self.welcome_canvas.create_line(
            self.cx, self.cy, self.cx, self.cy - 250,
            fill="#00ff88",
            width=3
        )
        
        # Create data points
        self.data_points = []
        for _ in range(25):
            angle = random.uniform(0, 360)
            dist = random.uniform(80, 250)
            self.data_points.append({
                'angle': angle,
                'dist': dist,
                'revealed': False,
                'item': None
            })
        
        # Start sweep
        self._animate_radar(0)
        
        # Start subtitle
        self.root.after(1800, self._phase4_subtitle)
    
    def _animate_radar(self, angle):
        """Animate radar sweep"""
        if angle > 720:  # Two rotations
            return
        
        try:
            import math
            
            # Update radar line
            rad = math.radians(angle)
            ex = self.cx + 250 * math.sin(rad)
            ey = self.cy - 250 * math.cos(rad)
            self.welcome_canvas.coords(self.radar_line, self.cx, self.cy, ex, ey)
            
            # Reveal data points
            current_angle = angle % 360
            for point in self.data_points:
                if not point['revealed'] and current_angle > point['angle']:
                    point['revealed'] = True
                    p_rad = math.radians(point['angle'])
                    px = self.cx + point['dist'] * math.sin(p_rad)
                    py = self.cy - point['dist'] * math.cos(p_rad)
                    
                    point['item'] = self.welcome_canvas.create_oval(
                        px-4, py-4, px+4, py+4,
                        fill="#00ff88",
                        outline="#00ff88",
                        width=2
                    )
            
            self.root.after(10, lambda: self._animate_radar(angle + 4))
        except:
            pass
    
    def _phase4_subtitle(self):
        """Subtitle with typing effect"""
        self.subtitle_text = "[ MAPPING YOUR COGNITIVE TERRAIN ]"
        self.subtitle = self.welcome_canvas.create_text(
            self.cx, self.cy + 60,
            text="",
            font=("Consolas", 14),
            fill="#00ff88",
            anchor="center"
        )
        
        self._type_text(0)
        
        # Transition to dashboard after subtitle completes
        subtitle_duration = len(self.subtitle_text) * 30  # 30ms per character
        self.root.after(subtitle_duration + 800, self._transition_to_dashboard)
    
    def _type_text(self, i):
        """Type subtitle"""
        if i > len(self.subtitle_text):
            return
        
        try:
            self.welcome_canvas.itemconfig(self.subtitle, text=self.subtitle_text[:i])
            self.root.after(30, lambda: self._type_text(i + 1))
        except:
            pass
    
    def _animate_typing(self, text: str, index: int):
        """Legacy - not used"""
        pass
    
    def _animate_loading(self, step: int):
        """Legacy - not used"""
        pass
    
    def _transition_to_dashboard(self):
        """Transition to main UI"""
        self.animation_running = False
        
        if hasattr(self, 'welcome_canvas'):
            self.welcome_canvas.destroy()
        
        self._build_main_ui()
        self._start_update_loops()

    def _build_main_ui(self):
        self.main_container = tk.Frame(self.root, bg=self.COLORS['bg_primary'])
        self.main_container.pack(fill="both", expand=True)
        
        self._create_sidebar()
        
        self.content_frame = tk.Frame(
            self.main_container, 
            bg=self.COLORS['bg_primary']
        )
        self.content_frame.pack(side="left", fill="both", expand=True)
        
        self._show_dashboard()
    
    def _create_sidebar(self):
        sidebar = tk.Frame(
            self.main_container,
            bg=self.COLORS['bg_secondary'],
            width=250
        )
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        
        logo_frame = tk.Frame(sidebar, bg=self.COLORS['bg_secondary'])
        logo_frame.pack(fill="x", pady=30, padx=20)
        
        tk.Label(
            logo_frame,
            text="⚡ Focus",
            font=("Segoe UI", 24, "bold"),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w")
        
        tk.Label(
            logo_frame,
            text="Intelligence",
            font=("Segoe UI", 14),
            fg=self.COLORS['text_secondary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w")
        
        nav_items = [
            ("📊 Dashboard", "dashboard"),
            ("📜 Session History", "history"),
            ("📈 Reports", "reports"),
        ]
        
        for label, view in nav_items:
            btn = self._create_nav_button(sidebar, label, view)
            btn.pack(fill="x", padx=15, pady=5)
            
            # --- NEW: PRIVACY INSPECTOR BUTTON ---
            privacy_btn = tk.Button(
                sidebar,
                text="🛡️ Privacy Inspector ",
                font=("Segoe UI", 11, "bold"),
                fg=self.COLORS['accent_green'],
                bg=self.COLORS['bg_tertiary'],
                activebackground=self.COLORS['bg_secondary'],
                activeforeground=self.COLORS['accent_green'],
                relief="flat",
                bd=0,
                padx=5,
                pady=12,
                cursor="hand2",
                command=self._toggle_privacy_terminal
            )
        privacy_btn.pack(side="bottom", fill="x", padx=15, pady=(0, 15))
        # ------------------------------------
        
        exit_btn = self._create_exit_button(sidebar)
        exit_btn.pack(side="bottom", fill="x", padx=15, pady=30)
    
    def _create_nav_button(self, parent, text: str, view: str):
        btn = tk.Button(
            parent,
            text=text,
            font=("Segoe UI", 11),
            fg=self.COLORS['text_secondary'],
            bg=self.COLORS['bg_secondary'],
            activebackground=self.COLORS['bg_tertiary'],
            activeforeground=self.COLORS['text_primary'],
            relief="flat",
            bd=0,
            padx=20,
            pady=12,
            anchor="w",
            cursor="hand2",
            command=lambda: self._switch_view(view)
        )
        
        btn.bind("<Enter>", lambda e: btn.configure(
            bg=self.COLORS['bg_tertiary'],
            fg=self.COLORS['text_primary']
        ))
        btn.bind("<Leave>", lambda e: btn.configure(
            bg=self.COLORS['bg_secondary'],
            fg=self.COLORS['text_secondary']
        ))
        
        return btn

    def _create_exit_button(self, parent):
        btn = tk.Button(
            parent,
            text="🚪 Exit Application",
            font=("Segoe UI", 12, "bold"),
            fg="#ffffff",
            bg=self.COLORS['danger'],
            activebackground="#b91c1c",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=20,
            pady=15,
            cursor="hand2",
            command=self._on_closing
        )
        
        btn.bind("<Enter>", lambda e: btn.configure(bg="#dc2626"))
        btn.bind("<Leave>", lambda e: btn.configure(bg=self.COLORS['danger']))
        btn.bind("<ButtonPress-1>", lambda e: btn.configure(bg="#b91c1c"))
        btn.bind("<ButtonRelease-1>", lambda e: btn.configure(bg="#dc2626"))
        
        return btn
    
    def _switch_view(self, view: str):
        self.current_view = view
        for widget in self.content_frame.winfo_children():
            widget.destroy()
        
        if view == "dashboard":
            self._show_dashboard()
        elif view == "history":
            self._show_history()
        elif view == "reports":
            self._show_reports()

    def _show_dashboard(self):
        header = tk.Frame(self.content_frame, bg=self.COLORS['bg_primary'])
        header.pack(fill="x", padx=30, pady=20)
        
        tk.Label(
            header,
            text="Dashboard Overview",
            font=("Segoe UI", 28, "bold"),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_primary']
        ).pack(side="left")
        
        self.clock_label = tk.Label(
            header,
            text=datetime.now().strftime("%I:%M:%S %p"),
            font=("Segoe UI", 14),
            fg=self.COLORS['accent_blue'],
            bg=self.COLORS['bg_primary']
        )
        self.clock_label.pack(side="right")
        
        metrics_container = tk.Frame(self.content_frame, bg=self.COLORS['bg_primary'])
        metrics_container.pack(fill="both", expand=True, padx=30, pady=(0, 20))
        
        for i in range(3):
            metrics_container.columnconfigure(i, weight=1)
        for i in range(3):
            metrics_container.rowconfigure(i, weight=1)
        
        self._create_session_duration_card(metrics_container, 0, 0)
        self._create_focus_level_card(metrics_container, 0, 1)
        self._create_system_status_card(metrics_container, 0, 2)
        
        self._create_focus_power_graph(metrics_container, 1, 0, 3)

    def _create_metric_card(self, parent, row, col, colspan=1):
        card = tk.Frame(
            parent,
            bg=self.COLORS['bg_secondary'],
            highlightbackground=self.COLORS['bg_tertiary'],
            highlightthickness=2
        )
        card.grid(
            row=row, 
            column=col, 
            columnspan=colspan,
            sticky="nsew", 
            padx=10, 
            pady=10
        )
        
        card.bind("<Enter>", lambda e: card.configure(
            highlightbackground=self.COLORS['accent_blue']
        ))
        card.bind("<Leave>", lambda e: card.configure(
            highlightbackground=self.COLORS['bg_tertiary']
        ))
        
        return card
    
    def _create_session_duration_card(self, parent, row, col):
        card = self._create_metric_card(parent, row, col)
        
        tk.Label(
            card,
            text="⏱ SESSION DURATION",
            font=("Segoe UI", 10, "bold"),
            fg=self.COLORS['text_tertiary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w", padx=20, pady=(20, 5))
        
        self.session_duration_value = tk.Label(
            card,
            text="00:00:00",
            font=("Segoe UI", 36, "bold"),
            fg=self.COLORS['accent_blue'],
            bg=self.COLORS['bg_secondary']
        )
        self.session_duration_value.pack(anchor="w", padx=20, pady=(0, 10))
        
        tk.Label(
            card,
            text="Active monitoring time",
            font=("Segoe UI", 9),
            fg=self.COLORS['text_secondary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w", padx=20, pady=(0, 20))
    
    def _create_focus_level_card(self, parent, row, col):
        card = self._create_metric_card(parent, row, col)
        
        tk.Label(
            card,
            text="🎯 FOCUS LEVEL",
            font=("Segoe UI", 10, "bold"),
            fg=self.COLORS['text_tertiary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w", padx=20, pady=(20, 5))
        
        self.focus_level_value = tk.Label(
            card,
            text="idle",
            font=("Segoe UI", 24, "bold"),
            fg=self.COLORS['text_secondary'],
            bg=self.COLORS['bg_secondary']
        )
        self.focus_level_value.pack(anchor="w", padx=20, pady=(10, 10))
        
        self.focus_dot = tk.Canvas(
            card,
            width=20,
            height=20,
            bg=self.COLORS['bg_secondary'],
            highlightthickness=0
        )
        self.focus_dot.pack(anchor="w", padx=20, pady=(0, 20))
        self.focus_dot.create_oval(2, 2, 18, 18, fill="#6C757D", outline="#6C757D")
    
    def _create_system_status_card(self, parent, row, col):
        card = self._create_metric_card(parent, row, col)
        
        tk.Label(
            card,
            text="💻 ACTIVE APP",
            font=("Segoe UI", 10, "bold"),
            fg=self.COLORS['text_tertiary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w", padx=20, pady=(20, 5))
        
        self.active_app_label = tk.Label(
            card,
            text="Unknown",
            font=("Segoe UI", 18, "bold"),
            fg=self.COLORS['accent_blue'],
            bg=self.COLORS['bg_secondary'],
            wraplength=200
        )
        self.active_app_label.pack(anchor="w", padx=20, pady=(10, 10))
        
        self.system_status_value = tk.Label(
            card,
            text="● Active",
            font=("Segoe UI", 12),
            fg=self.COLORS['success'],
            bg=self.COLORS['bg_secondary']
        )
        self.system_status_value.pack(anchor="w", padx=20, pady=(0, 20))

    def _create_focus_power_graph(self, parent, row, col, colspan):
        card = self._create_metric_card(parent, row, col, colspan)
        
        header_frame = tk.Frame(card, bg=self.COLORS['bg_secondary'])
        header_frame.pack(fill="x", padx=20, pady=(20, 5))
        
        tk.Label(
            header_frame,
            text="⚡ FOCUS ENERGY FLOW",
            font=("Segoe UI", 16, "bold"),
            fg=self.COLORS['accent_blue'],
            bg=self.COLORS['bg_secondary']
        ).pack(side="left")
        
        self.focus_status_label = tk.Label(
            header_frame,
            text="● OPTIMAL",
            font=("Segoe UI", 10, "bold"),
            fg=self.COLORS['accent_green'],
            bg=self.COLORS['bg_secondary']
        )
        self.focus_status_label.pack(side="right")
        
        self.focus_power_percentage = tk.Label(
            card,
            text="100%",
            font=("Segoe UI", 32, "bold"),
            fg=self.COLORS['accent_green'],
            bg=self.COLORS['bg_secondary']
        )
        self.focus_power_percentage.pack(anchor="w", padx=20, pady=(5, 10))
        
        self.focus_fig = Figure(figsize=(12, 3.5), dpi=100, facecolor=self.COLORS['bg_secondary'])
        self.focus_ax = self.focus_fig.add_subplot(111)
        self.focus_ax.set_facecolor('#0a0f1e') 
        
        self.focus_power_data = deque(maxlen=60)
        self._load_focus_power_history()
        
        if len(self.focus_power_data) == 0:
            current_battery = self.engine.last_battery
            for _ in range(60):
                self.focus_power_data.append(current_battery)
        
        x_data = list(range(len(self.focus_power_data)))
        y_data = list(self.focus_power_data)
        
        current_battery = self.engine.last_battery
        if current_battery > 75:
            line_color = '#10b981'
            fill_color = '#10b981'
        elif current_battery > 50:
            line_color = '#3b82f6'
            fill_color = '#3b82f6'
        elif current_battery > 25:
            line_color = '#f59e0b'
            fill_color = '#f59e0b'
        else:
            line_color = '#ef4444'
            fill_color = '#ef4444'
        
        self.focus_line, = self.focus_ax.plot(
            x_data,
            y_data,
            color=line_color,
            linewidth=3,
            marker='o',
            markersize=5,
            markerfacecolor=line_color,
            markeredgecolor='white',
            markeredgewidth=1.5,
            alpha=0.9,
            zorder=3
        )
        
        self.focus_fill = self.focus_ax.fill_between(
            x_data,
            y_data,
            0,
            alpha=0.25,
            color=fill_color,
            zorder=1
        )
        
        self.focus_ax.plot(
            x_data,
            y_data,
            color=line_color,
            linewidth=8,
            alpha=0.15,
            zorder=2
        )
        
        self.focus_ax.axhspan(75, 100, alpha=0.05, color='#10b981', zorder=0)
        self.focus_ax.axhspan(50, 75, alpha=0.05, color='#3b82f6', zorder=0)
        self.focus_ax.axhspan(25, 50, alpha=0.05, color='#f59e0b', zorder=0)
        self.focus_ax.axhspan(0, 25, alpha=0.05, color='#ef4444', zorder=0)
        
        self.focus_ax.axhline(y=75, color='#10b981', linestyle='--', alpha=0.4, linewidth=1.5)
        self.focus_ax.axhline(y=50, color='#3b82f6', linestyle='--', alpha=0.4, linewidth=1.5)
        self.focus_ax.axhline(y=25, color='#ef4444', linestyle='--', alpha=0.4, linewidth=1.5)
        
        # Labels positioned outside the graph (x=61, xlim is 60)
        self.focus_ax.text(61, 87, 'PEAK', fontsize=8, color='#10b981', alpha=0.7, weight='bold', ha='left')
        self.focus_ax.text(61, 62, 'GOOD', fontsize=8, color='#3b82f6', alpha=0.7, weight='bold', ha='left')
        self.focus_ax.text(61, 37, 'LOW', fontsize=8, color='#f59e0b', alpha=0.7, weight='bold', ha='left')
        self.focus_ax.text(61, 12, 'CRITICAL', fontsize=8, color='#ef4444', alpha=0.7, weight='bold', ha='left')
        
        self.focus_ax.set_xlim(-1, 60)
        self.focus_ax.set_ylim(0, 100)
        self.focus_ax.set_xlabel("← Past  |  Time Flow (2-second intervals)  |  Now →", 
                                  color='#64748b', fontsize=10, style='italic')
        self.focus_ax.set_ylabel("Energy Level (%)", color='#94a3b8', fontsize=11, weight='bold')
        self.focus_ax.grid(True, alpha=0.1, color='#475569', linestyle=':')
        self.focus_ax.tick_params(colors='#64748b', labelsize=9)
        
        self.focus_ax.spines['top'].set_visible(False)
        self.focus_ax.spines['right'].set_visible(False)
        self.focus_ax.spines['left'].set_color('#334155')
        self.focus_ax.spines['bottom'].set_color('#334155')
        self.focus_ax.spines['left'].set_linewidth(2)
        self.focus_ax.spines['bottom'].set_linewidth(2)
        
        self.focus_graph_canvas = FigureCanvasTkAgg(self.focus_fig, master=card)
        self.focus_graph_canvas.draw()
        self.focus_graph_canvas.get_tk_widget().pack(fill="both", expand=True, padx=20, pady=(0, 20))
    
    def _load_focus_power_history(self):
        conn = None
        try:
            conn = sqlite3.connect(self.engine.db_name, timeout=10.0)
            cursor = conn.cursor()
            
            two_minutes_ago = datetime.now() - timedelta(minutes=2)
            cursor.execute(
                "SELECT battery FROM logs WHERE time_full >= ? ORDER BY time_full ASC",
                (two_minutes_ago.strftime("%Y-%m-%d %H:%M:%S"),)
            )
            
            rows = cursor.fetchall()
            
            for row in rows:
                self.focus_power_data.append(row[0])
            
            logging.info(f"Loaded {len(self.focus_power_data)} focus power data points from database")
            
        except Exception as e:
            logging.error(f"Error loading focus power history: {e}")
        finally:
            if conn:
                conn.close()

    def _show_history(self):
        header_frame = tk.Frame(self.content_frame, bg=self.COLORS['bg_primary'])
        header_frame.pack(fill="x", padx=30, pady=20) 
        
        title_section = tk.Frame(header_frame, bg=self.COLORS['bg_primary'])
        title_section.pack(fill="x", expand=True)
        
        tk.Label(title_section, text="📊 SESSION ATTENTION FLOW", font=("Segoe UI", 32, "bold"), fg=self.COLORS['accent_blue'], bg=self.COLORS['bg_primary']).pack(anchor="w")
        tk.Label(title_section, text="Continuous Session Timeline • 1 Block = 5 Seconds • 1 Row = 2 Minutes", font=("Segoe UI", 12), fg=self.COLORS['text_tertiary'], bg=self.COLORS['bg_primary']).pack(anchor="w", pady=(5, 0))
        
        # Container for the Heatmap
        heatmap_container = tk.Frame(self.content_frame, bg=self.COLORS['bg_secondary'], highlightbackground=self.COLORS['bg_tertiary'], highlightthickness=2)
        heatmap_container.pack(fill="both", expand=True, padx=30, pady=10)
        
        # ------------------------------------------------------------------
        # 1. DRAW LEGEND FIRST (Locked to the bottom so it never hides)
        # ------------------------------------------------------------------
        legend_frame = tk.Frame(heatmap_container, bg=self.COLORS['bg_secondary'])
        legend_frame.pack(side="bottom", fill="x", padx=20, pady=(10, 20))
        
        legend_center = tk.Frame(legend_frame, bg=self.COLORS['bg_secondary'])
        legend_center.pack(expand=True)

        states = [
            ("Deep Focus", self.STATE_COLORS.get("focus", "#8b5cf6")),
            ("Low Focus", self.STATE_COLORS.get("lowfocus", "#f59e0b")),
            ("Not Attentive", self.STATE_COLORS.get("not attentive", "#ef4444")),
            ("Switching", self.STATE_COLORS.get("switching", "#ec4899")),
            ("Drift", self.STATE_COLORS.get("drift", "#6b7280")),
            ("Media", self.STATE_COLORS.get("Media Consumption", "#3b82f6")),
            ("Idle", self.STATE_COLORS.get("idle", "#10b981"))
        ]
        
        for name, color in states:
            item = tk.Frame(legend_center, bg=self.COLORS['bg_secondary'])
            item.pack(side="left", padx=15)
            tk.Frame(item, width=18, height=18, bg=color, highlightthickness=0).pack(side="left", padx=(0, 8))
            tk.Label(item, text=name, font=("Segoe UI", 12, "bold"), fg=self.COLORS['text_primary'], bg=self.COLORS['bg_secondary']).pack(side="left")

        # ------------------------------------------------------------------
        # 2. DRAW GRID SECOND (Fills the remaining space in the middle)
        # ------------------------------------------------------------------
        center_frame = tk.Frame(heatmap_container, bg=self.COLORS['bg_secondary'])
        center_frame.pack(expand=True, pady=15)

        grid_container = tk.Frame(center_frame, bg=self.COLORS['bg_secondary'])
        grid_container.pack(padx=20)
        
        # Create a Sequential Grid (24 cols = 2 mins, 30 rows = 60 mins/1 hr)
        self.history_heatmap_blocks = []
        ROWS = 30
        COLS = 24 
        
        self.empty_block_color = "#2a3649" 
        
        for r in range(ROWS):
            for c in range(COLS):
                block = tk.Frame(
                    grid_container, 
                    width=20,  # Beautiful large block size
                    height=20, 
                    bg=self.empty_block_color, 
                    highlightthickness=0 
                )
                block.grid(row=r, column=c, padx=2, pady=2) # 2px crisp negative space
                self.history_heatmap_blocks.append(block) 
                

        # Initial Load
        self._update_history_view()
    def _toggle_privacy_terminal(self):
        """Opens a live terminal to prove data content is being destroyed"""
        # If it's already open, close it
        if hasattr(self, 'privacy_window') and self.privacy_window.winfo_exists():
            self.privacy_window.destroy()
            return

        # Create a new top-level window
        self.privacy_window = tk.Toplevel(self.root)
        self.privacy_window.title("Glass Box Privacy Inspector")
        self.privacy_window.geometry("450x350")
        self.privacy_window.configure(bg="#0a0a0a") # Deep terminal black
        self.privacy_window.attributes("-topmost", True) # Keep it floating on top

        # Header
        header = tk.Label(
            self.privacy_window, 
            text="[ZERO-KNOWLEDGE TELEMETRY STREAM]", 
            font=("Consolas", 10, "bold"), 
            fg="#10b981", 
            bg="#0a0a0a", 
            anchor="w"
        )
        header.pack(fill="x", padx=10, pady=(10, 0))

        # Terminal Text Box
        self.terminal_text = tk.Text(
            self.privacy_window, 
            font=("Consolas", 9), 
            fg="#10b981", # Hacker Green
            bg="#0a0a0a", 
            bd=0, 
            highlightthickness=0, 
            state="disabled"
        )
        self.terminal_text.pack(fill="both", expand=True, padx=10, pady=10)

        # Start the data loop
        self._update_privacy_terminal()
        
    def _toggle_privacy_terminal(self):
        """Opens a live terminal to prove data content is being destroyed"""
        # If it's already open, close it
        if hasattr(self, 'privacy_window') and self.privacy_window.winfo_exists():
            self.privacy_window.destroy()
            return

        # Create a new top-level window
        self.privacy_window = tk.Toplevel(self.root)
        self.privacy_window.title("Glass Box Privacy Inspector")
        self.privacy_window.geometry("450x350")
        self.privacy_window.configure(bg="#0a0a0a") # Deep terminal black
        self.privacy_window.attributes("-topmost", True) # Keep it floating on top

        # Header
        header = tk.Label(
            self.privacy_window, 
            text="[ZERO-KNOWLEDGE TELEMETRY STREAM]", 
            font=("Consolas", 10, "bold"), 
            fg="#10b981", 
            bg="#0a0a0a", 
            anchor="w"
        )
        header.pack(fill="x", padx=10, pady=(10, 0))

        # Terminal Text Box
        self.terminal_text = tk.Text(
            self.privacy_window, 
            font=("Consolas", 9), 
            fg="#10b981", # Hacker Green
            bg="#0a0a0a", 
            bd=0, 
            highlightthickness=0, 
            state="disabled"
        )
        self.terminal_text.pack(fill="both", expand=True, padx=10, pady=10)

        # Start the data loop
        self._update_privacy_terminal()

    def _update_privacy_terminal(self):
        """Fetches live memory variables and prints them to the terminal"""
        if not hasattr(self, 'privacy_window') or not self.privacy_window.winfo_exists():
            return

        # 1. Grab raw data directly from the ML engine
        app = self.engine.last_app
        raw_k = self.engine.smooth_keys
        raw_m = self.engine.smooth_mouse
        switches = len(self.engine.app_switch_timestamps)
        state = self.engine.last_state.upper()

        # 2. Format the output to emphasize Privacy & Destruction
        log_entry = f"> MEMORY BUFFER: SECURE PURGE EXEC\n"
        log_entry += f"  Target App:   '{app}'\n"
        log_entry += f"  Key Cadence:  {raw_k:.2f} hz (CONTENT DESTROYED)\n"
        log_entry += f"  Mouse Veloc:  {raw_m:.2f} hz (COORDS IGNORED)\n"
        log_entry += f"  Ctx Switches: {switches} (Trailing 60s)\n"
        log_entry += f"  ML Inference: [ {state} ]\n"
        log_entry += f"----------------------------------------\n"

        # 3. Insert into the terminal UI
        self.terminal_text.config(state="normal")
        self.terminal_text.insert("end", log_entry)
        self.terminal_text.see("end") # Auto-scroll to the bottom
        
        # 4. Keep memory light (only keep last 40 lines)
        lines = int(self.terminal_text.index('end-1c').split('.')[0])
        if lines > 40:
            self.terminal_text.delete("1.0", f"{lines-40}.0")
            
        self.terminal_text.config(state="disabled")

        # 5. Loop this exact function every 3 seconds (reduced from 2s)
        self.privacy_window.after(3000, self._update_privacy_terminal)
        
    def _get_heatmap_data(self):
        """Query DB to find dominant state in every 10 min block for today"""
        today = datetime.now().strftime("%Y-%m-%d")
        grid = [["No Data" for _ in range(6)] for _ in range(24)]
        
        conn = None
        try:
            conn = sqlite3.connect(self.engine.db_name, timeout=10.0)
            cursor = conn.cursor()
            cursor.execute("SELECT time_full, state FROM logs WHERE time_full LIKE ?", (today + '%',))
            rows = cursor.fetchall()
            
            blocks = {}
            for row in rows:
                dt_str, state = row
                try:
                    time_part = dt_str.split(" ")[1]
                    h = int(time_part.split(":")[0])
                    m = int(time_part.split(":")[1]) // 10
                    
                    if (h, m) not in blocks:
                        blocks[(h, m)] = {}
                    blocks[(h, m)][state] = blocks[(h, m)].get(state, 0) + 1
                except:
                    pass
                    
            for (h, m), state_counts in blocks.items():
                dominant_state = max(state_counts, key=state_counts.get)
                grid[h][m] = dominant_state
        except Exception as e:
            logging.error(f"Heatmap Gen Error: {e}")
        finally:
            if conn:
                conn.close()
        return grid
    
    def _create_history_card(self, parent, entry, pack_at_top=False):
        timestamp, state = entry
        color = self.STATE_COLORS.get(state, "#6C757D")
        
        # Emoji Map corresponding to your states
        state_emojis = { 
            "idle": "🟢", 
            "Media Consumption": "📺", 
            "lowfocus": "🟡", 
            "focus": "🟣", 
            "not attentive": "🔴", 
            "switching": "🔵",
            "drift": "🌫️"  # Cognitive Drift
        }
        emoji = state_emojis.get(state, "⚪")
        
        container = tk.Frame(parent, bg=self.COLORS['bg_primary'])
        if pack_at_top and parent.pack_slaves(): container.pack(before=parent.pack_slaves()[0], fill="x", pady=8, padx=20)
        else: container.pack(fill="x", pady=8, padx=20)
        
        timeline_frame = tk.Frame(container, bg=self.COLORS['bg_primary'], width=60)
        timeline_frame.pack(side="left", fill="y")
        timeline_frame.pack_propagate(False)
        
        dot_canvas = tk.Canvas(timeline_frame, width=40, height=40, bg=self.COLORS['bg_primary'], highlightthickness=0)
        dot_canvas.pack(pady=10)
        dot_canvas.create_oval(5, 5, 35, 35, fill=color, outline=color, width=0)
        dot_canvas.create_oval(10, 10, 30, 30, fill=color, outline='white', width=2)
        
        if not pack_at_top or len(parent.pack_slaves()) > 1:
            line_canvas = tk.Canvas(timeline_frame, width=4, height=100, bg=self.COLORS['bg_primary'], highlightthickness=0)
            line_canvas.pack()
            line_canvas.create_line(2, 0, 2, 100, fill=self.COLORS['bg_tertiary'], width=3)
        
        card = tk.Frame(container, bg=self.COLORS['bg_secondary'], highlightbackground=color, highlightthickness=3)
        card.pack(side="left", fill="both", expand=True)
        
        card.bind("<Enter>", lambda e: card.configure(highlightbackground=color, highlightthickness=4))
        card.bind("<Leave>", lambda e: card.configure(highlightbackground=color, highlightthickness=3))
        
        content_frame = tk.Frame(card, bg=self.COLORS['bg_secondary'])
        content_frame.pack(fill="both", expand=True, padx=20, pady=15)
        
        top_row = tk.Frame(content_frame, bg=self.COLORS['bg_secondary'])
        top_row.pack(fill="x", pady=(0, 10))
        
        time_str = timestamp.strftime("%I:%M:%S %p")
        time_label = tk.Label(top_row, text=f"🕐 {time_str}", font=("Segoe UI", 11, "bold"), fg=self.COLORS['text_tertiary'], bg=self.COLORS['bg_secondary'])
        time_label.pack(side="left")
        
        age = (datetime.now() - timestamp).total_seconds()
        age_text = f"{int(age)}s ago" if age < 60 else f"{int(age/60)}m ago" if age < 3600 else f"{int(age/3600)}h ago"
        age_color = self.COLORS['accent_green'] if age < 60 else self.COLORS['accent_blue'] if age < 3600 else self.COLORS['text_tertiary']
        
        age_label = tk.Label(top_row, text=age_text, font=("Segoe UI", 10), fg=age_color, bg=self.COLORS['bg_secondary'])
        age_label.pack(side="right")
        
        state_row = tk.Frame(content_frame, bg=self.COLORS['bg_secondary'])
        state_row.pack(fill="x")
        
        tk.Label(state_row, text=emoji, font=("Segoe UI", 24), bg=self.COLORS['bg_secondary']).pack(side="left", padx=(0, 10))
        
        # Format the display string neatly
        display_state = state.title() if state != "lowfocus" else "Low Focus"
        
        state_label = tk.Label(state_row, text=display_state, font=("Segoe UI", 20, "bold"), fg=color, bg=self.COLORS['bg_secondary'])
        state_label.pack(side="left")
        
        # Privacy-conscious badges (No "Productivity" framing)
        badge_text = {
            "focus": "ANCHORED", 
            "lowfocus": "ACTIVE", 
            "idle": "RESTING", 
            "not attentive": "FRAGMENTED", 
            "switching": "MULTITASKING", 
            "Media Consumption": "CONSUMING",
            "drift": "DRIFTING"  # Cognitive Drift
        }.get(state, "ACTIVE")
        
        badge = tk.Label(state_row, text=badge_text, font=("Segoe UI", 9, "bold"), fg=self.COLORS['bg_secondary'], bg=color, padx=10, pady=3)
        badge.pack(side="right")
        
        card_data = (card, time_label, state_label, age_label, timestamp, state)
        if pack_at_top: self.history_labels.insert(0, card_data)
        else: self.history_labels.append(card_data)
        return card_data

    def _show_reports(self):
        tk.Label(
            self.content_frame,
            text="📈 Productivity Reports",
            font=("Segoe UI", 28, "bold"),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_primary']
        ).pack(padx=30, pady=30)
        
        selector_frame = tk.Frame(self.content_frame, bg=self.COLORS['bg_primary'])
        selector_frame.pack(fill="x", padx=30, pady=(0, 20))
        
        self.report_type = tk.StringVar(value="daily")
        
        btn_frame = tk.Frame(selector_frame, bg=self.COLORS['bg_secondary'])
        btn_frame.pack(fill="x", pady=10)
        
        for report_type, label in [("daily", "📅 Daily"), ("weekly", "📊 Weekly"), ("monthly", "📆 Monthly")]:
            btn = tk.Button(
                btn_frame,
                text=label,
                font=("Segoe UI", 12, "bold"),
                fg=self.COLORS['text_primary'],
                bg=self.COLORS['bg_tertiary'],
                activebackground=self.COLORS['accent_blue'],
                relief="flat",
                bd=0,
                padx=30,
                pady=12,
                cursor="hand2",
                command=lambda rt=report_type: self._load_report(rt)
            )
            btn.pack(side="left", padx=10, pady=10)
        
        self.report_scroll = ScrollableFrame(self.content_frame, bg_color=self.COLORS['bg_primary'])
        self.report_scroll.pack(fill="both", expand=True, padx=30, pady=20)
        
        self.report_container = self.report_scroll.scrollable_frame
        
        self._load_report("daily")
    
    def _load_report(self, report_type: str):
        self.report_type.set(report_type)
        
        for widget in self.report_container.winfo_children():
            widget.destroy()
        
        report_data = self._generate_report_data(report_type)
        self._display_report(report_type, report_data)
    
    def _generate_report_data(self, report_type: str) -> Dict:
        conn = None
        try:
            conn = sqlite3.connect(self.engine.db_name, timeout=10.0)
            cursor = conn.cursor()
            
            now = datetime.now()
            if report_type == "daily":
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
                title = f"Daily Report - {now.strftime('%B %d, %Y')}"
            elif report_type == "weekly":
                start_date = now - timedelta(days=now.weekday())
                start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
                title = f"Weekly Report - Week of {start_date.strftime('%B %d, %Y')}"
            else: 
                start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                title = f"Monthly Report - {now.strftime('%B %Y')}"
            
            cursor.execute(
                "SELECT time_full, state, active_app, interval, battery FROM logs WHERE time_full >= ? ORDER BY time_full",
                (start_date.strftime("%Y-%m-%d %H:%M:%S"),)
            )
            logs = cursor.fetchall()
            
            total_time = 0
            state_times = {}
            app_times = {}
            battery_levels = []
            
            for log in logs:
                time_str, state, app, interval, battery = log
                total_time += interval
                
                if state not in state_times:
                    state_times[state] = 0
                state_times[state] += interval
                
                if app not in app_times:
                    app_times[app] = 0
                app_times[app] += interval
                
                battery_levels.append(battery)
            
            avg_battery = sum(battery_levels) / len(battery_levels) if battery_levels else 0
            top_apps = sorted(app_times.items(), key=lambda x: x[1], reverse=True)[:5]
            
            deep_focus_time = state_times.get("focus", 0)
            reading_time = state_times.get("lowfocus", 0)
            productive_time = deep_focus_time + reading_time
            productivity_score = (productive_time / total_time * 100) if total_time > 0 else 0
            
            return {
                "title": title,
                "total_time": total_time,
                "state_times": state_times,
                "top_apps": top_apps,
                "avg_battery": avg_battery,
                "productivity_score": productivity_score,
                "total_sessions": len(logs),
                "deep_focus_time": deep_focus_time,
                "reading_time": reading_time,
            }
            
        except Exception as e:
            logging.error(f"Error generating report: {e}")
            return {
                "title": f"{report_type.capitalize()} Report",
                "total_time": 0,
                "state_times": {},
                "top_apps": [],
                "avg_battery": 0,
                "productivity_score": 0,
                "total_sessions": 0,
                "deep_focus_time": 0,
                "reading_time": 0,
            }
        finally:
            if conn:
                conn.close()
    
    def _display_report(self, report_type: str, data: Dict):
        tk.Label(
            self.report_container,
            text=data["title"],
            font=("Segoe UI", 20, "bold"),
            fg=self.COLORS['accent_blue'],
            bg=self.COLORS['bg_primary']
        ).pack(pady=(0, 20))
        
        summary_frame = tk.Frame(self.report_container, bg=self.COLORS['bg_primary'])
        summary_frame.pack(fill="x", pady=(0, 20))
        
        for i in range(1):
            summary_frame.columnconfigure(i, weight=1)
        
        self._create_summary_card(
            summary_frame, 0, 0,
            "⏱ Total Time",
            self._format_time(data["total_time"]),
            self.COLORS['accent_blue']
        )
        
        state_frame = tk.Frame(self.report_container, bg=self.COLORS['bg_secondary'])
        state_frame.pack(fill="x", pady=(0, 20))
        
        tk.Label(
            state_frame,
            text="Focus State Breakdown",
            font=("Segoe UI", 16, "bold"),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w", padx=20, pady=(20, 10))
        
        for state, time in sorted(data["state_times"].items(), key=lambda x: x[1], reverse=True):
            self._create_state_row(state_frame, state, time, data["total_time"])
        
        apps_frame = tk.Frame(self.report_container, bg=self.COLORS['bg_secondary'])
        apps_frame.pack(fill="x", pady=(0, 20), padx=10)
        
        tk.Label(
            apps_frame,
            text="📱 Top Applications",
            font=("Segoe UI", 16, "bold"),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w", padx=20, pady=(20, 10))
        
        if data["top_apps"] and len(data["top_apps"]) > 0:
            for i, (app, time) in enumerate(data["top_apps"], 1):
                self._create_app_row(apps_frame, i, app, time)
            tk.Label(
                apps_frame,
                text="",
                bg=self.COLORS['bg_secondary']
            ).pack(pady=10)
        else:
            tk.Label(
                apps_frame,
                text="No application data available yet. Keep working to see your top apps!",
                font=("Segoe UI", 11),
                fg=self.COLORS['text_tertiary'],
                bg=self.COLORS['bg_secondary'],
                wraplength=800,
                justify="left"
            ).pack(anchor="w", padx=40, pady=(5, 20))
        
        insights_frame = tk.Frame(self.report_container, bg=self.COLORS['bg_secondary'])
        insights_frame.pack(fill="x", pady=(0, 20))
        
        tk.Label(
            insights_frame,
            text="💡 Insights",
            font=("Segoe UI", 16, "bold"),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_secondary']
        ).pack(anchor="w", padx=20, pady=(20, 10))
        
        insights = self._generate_insights(data)
        for insight in insights:
            tk.Label(
                insights_frame,
                text=f"• {insight}",
                font=("Segoe UI", 11),
                fg=self.COLORS['text_secondary'],
                bg=self.COLORS['bg_secondary'],
                wraplength=800,
                justify="left"
            ).pack(anchor="w", padx=40, pady=5)
        
        tk.Label(
            insights_frame,
            text="",
            bg=self.COLORS['bg_secondary']
        ).pack(pady=10)
    
    def _create_summary_card(self, parent, row, col, title, value, color):
        card = tk.Frame(
            parent,
            bg=self.COLORS['bg_secondary'],
            highlightbackground=self.COLORS['bg_tertiary'],
            highlightthickness=2
        )
        card.grid(row=row, column=col, sticky="nsew", padx=10, pady=10)
        
        tk.Label(
            card,
            text=title,
            font=("Segoe UI", 10, "bold"),
            fg=self.COLORS['text_tertiary'],
            bg=self.COLORS['bg_secondary']
        ).pack(pady=(20, 5))
        
        tk.Label(
            card,
            text=value,
            font=("Segoe UI", 28, "bold"),
            fg=color,
            bg=self.COLORS['bg_secondary']
        ).pack(pady=(0, 20))
    
    def _create_state_row(self, parent, state, time, total_time):
        row = tk.Frame(parent, bg=self.COLORS['bg_secondary'])
        row.pack(fill="x", padx=20, pady=5)
        
        color = self.STATE_COLORS.get(state, "#6C757D")
        dot = tk.Canvas(row, width=16, height=16, bg=self.COLORS['bg_secondary'], highlightthickness=0)
        dot.pack(side="left", padx=(0, 10))
        dot.create_oval(2, 2, 14, 14, fill=color, outline=color)
        
        tk.Label(
            row,
            text=state.title(),
            font=("Segoe UI", 11),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_secondary'],
            width=25,
            anchor="w"
        ).pack(side="left")
        
        tk.Label(
            row,
            text=self._format_time(time),
            font=("Segoe UI", 11),
            fg=self.COLORS['text_secondary'],
            bg=self.COLORS['bg_secondary'],
            width=15,
            anchor="w"
        ).pack(side="left")
        
        percentage = (time / total_time * 100) if total_time > 0 else 0
        tk.Label(
            row,
            text=f"{percentage:.1f}%",
            font=("Segoe UI", 11, "bold"),
            fg=color,
            bg=self.COLORS['bg_secondary']
        ).pack(side="right", padx=20)
    
    def _create_app_row(self, parent, rank, app, time):
        row = tk.Frame(parent, bg=self.COLORS['bg_secondary'])
        row.pack(fill="x", padx=20, pady=5)
        
        tk.Label(
            row,
            text=f"#{rank}",
            font=("Segoe UI", 11, "bold"),
            fg=self.COLORS['accent_blue'],
            bg=self.COLORS['bg_secondary'],
            width=5
        ).pack(side="left")
        
        tk.Label(
            row,
            text=app,
            font=("Segoe UI", 11),
            fg=self.COLORS['text_primary'],
            bg=self.COLORS['bg_secondary'],
            width=40,
            anchor="w"
        ).pack(side="left")
        
        tk.Label(
            row,
            text=self._format_time(time),
            font=("Segoe UI", 11),
            fg=self.COLORS['text_secondary'],
            bg=self.COLORS['bg_secondary']
        ).pack(side="right", padx=20)
    
    def _format_time(self, seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"
    
    def _get_battery_color(self, battery: float) -> str:
        if battery >= 75:
            return self.COLORS['accent_green']
        elif battery >= 50:
            return self.COLORS['accent_blue']
        elif battery >= 25:
            return self.COLORS['accent_yellow']
        else:
            return self.COLORS['accent_red']
    
    def _generate_insights(self, data: Dict) -> List[str]:
        insights = []
        
        if data["total_time"] > 0:
            hours = int(data["total_time"] / 3600)
            if hours > 0:
                insights.append(f"You spent {hours} hour{'s' if hours != 1 else ''} being monitored during this period.")
        
        if data["productivity_score"] > 75:
            insights.append("Excellent focus! You maintained high productivity throughout this period.")
        elif data["productivity_score"] > 50:
            insights.append("Good work! Your focus levels were above average.")
        elif data["productivity_score"] > 25:
            insights.append("Room for improvement. Try to minimize distractions for better focus.")
        else:
            insights.append("Low focus detected. Consider taking breaks and reducing multitasking.")
        
        if data["top_apps"] and len(data["top_apps"]) > 0:
            top_app, top_time = data["top_apps"][0]
            top_hours = int(top_time / 3600)
            top_minutes = int((top_time % 3600) / 60)
            if top_hours > 0:
                time_str = f"{top_hours}h {top_minutes}m"
            else:
                time_str = f"{top_minutes}m"
            insights.append(f"Most used application: {top_app} ({time_str})")
        
        if data["state_times"]:
            dominant_state = max(data["state_times"].items(), key=lambda x: x[1])
            state_name, state_time = dominant_state
            state_pct = int((state_time / data["total_time"] * 100)) if data["total_time"] > 0 else 0
            insights.append(f"You were in '{state_name.title()}' state {state_pct}% of the time.")
        
        if data["avg_battery"] >= 75:
            insights.append("Your focus energy remained high throughout this period. Great job!")
        elif data["avg_battery"] >= 50:
            insights.append("Your focus energy was moderate. Consider taking regular breaks.")
        elif data["avg_battery"] >= 25:
            insights.append("Your focus energy was low. Try to improve work-life balance.")
        else:
            insights.append("Critical focus energy levels detected. Take time to rest and recharge.")
        
        return insights if insights else ["No insights available yet. Keep working to generate meaningful data!"]
    
    def _start_update_loops(self):
        self._update_dashboard_metrics()
        self._update_focus_power_graph()
        self._update_clock()
        self._update_session_stats()
        self._update_state_history()
    
    def _update_dashboard_metrics(self):
        if self.current_view == "dashboard":
            duration = datetime.now() - self.engine.session_start
            hours, remainder = divmod(int(duration.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            self.session_duration_value.configure(text=f"{hours:02d}:{minutes:02d}:{seconds:02d}")
            self.session_stats['session_minutes'] = int(duration.total_seconds() / 60)
            
            state = self.engine.last_state
            
            if self.engine.paused:
                display_state = "Paused"
                color = self.COLORS['warning']
            else:
                display_state = state.title()
                color = self.STATE_COLORS.get(state, "#6C757D")
            
            self.focus_level_value.configure(text=display_state, fg=color)
            self.focus_dot.delete("all")
            self.focus_dot.create_oval(2, 2, 18, 18, fill=color, outline=color)
            
            if self.engine.paused:
                self.system_status_value.configure(text="⏸ Paused", fg=self.COLORS['warning'])
            else:
                self.system_status_value.configure(text="● Active", fg=self.COLORS['success'])
            
            app_name = self.engine.last_app if self.engine.last_app else "Unknown"
            self.active_app_label.configure(text=app_name)
        
        self.root.after(1000, self._update_dashboard_metrics)
    
    def _update_focus_power_graph(self):
        try:
            if self.current_view == "dashboard":
                if hasattr(self, 'focus_graph_canvas') and self.focus_graph_canvas.get_tk_widget().winfo_exists():
                    
                    current_battery = self.engine.last_battery
                    self.focus_power_data.append(current_battery)
                    
                    if current_battery > 75:
                        color = '#10b981'  
                        status_text = "● OPTIMAL"
                        status_color = self.COLORS['accent_green']
                    elif current_battery > 50:
                        color = '#3b82f6'  
                        status_text = "● GOOD"
                        status_color = self.COLORS['accent_blue']
                    elif current_battery > 25:
                        color = '#f59e0b'  
                        status_text = "● LOW"
                        status_color = self.COLORS['accent_yellow']
                    else:
                        color = '#ef4444'  
                        status_text = "● CRITICAL"
                        status_color = self.COLORS['accent_red']
                    
                    if hasattr(self, 'focus_status_label') and self.focus_status_label.winfo_exists():
                        self.focus_status_label.configure(text=status_text, fg=status_color)
                    
                    if hasattr(self, 'focus_power_percentage') and self.focus_power_percentage.winfo_exists():
                        self.focus_power_percentage.configure(text=f"{int(current_battery)}%", fg=color)
                    
                    self.focus_ax.clear()
                    
                    x_data = list(range(len(self.focus_power_data)))
                    y_data = list(self.focus_power_data)
                    
                    self.focus_ax.axhspan(75, 100, alpha=0.05, color='#10b981', zorder=0)
                    self.focus_ax.axhspan(50, 75, alpha=0.05, color='#3b82f6', zorder=0)
                    self.focus_ax.axhspan(25, 50, alpha=0.05, color='#f59e0b', zorder=0)
                    self.focus_ax.axhspan(0, 25, alpha=0.05, color='#ef4444', zorder=0)
                    
                    self.focus_ax.axhline(y=75, color='#10b981', linestyle='--', alpha=0.4, linewidth=1.5)
                    self.focus_ax.axhline(y=50, color='#3b82f6', linestyle='--', alpha=0.4, linewidth=1.5)
                    self.focus_ax.axhline(y=25, color='#ef4444', linestyle='--', alpha=0.4, linewidth=1.5)
                    
                    # Labels positioned outside the graph (x=61, xlim is 60)
                    self.focus_ax.text(61, 87, 'PEAK', fontsize=8, color='#10b981', alpha=0.7, weight='bold', ha='left')
                    self.focus_ax.text(61, 62, 'GOOD', fontsize=8, color='#3b82f6', alpha=0.7, weight='bold', ha='left')
                    self.focus_ax.text(61, 37, 'LOW', fontsize=8, color='#f59e0b', alpha=0.7, weight='bold', ha='left')
                    self.focus_ax.text(61, 12, 'CRITICAL', fontsize=8, color='#ef4444', alpha=0.7, weight='bold', ha='left')
                    
                    self.focus_ax.plot(
                        x_data,
                        y_data,
                        color=color,
                        linewidth=8,
                        alpha=0.15,
                        zorder=2
                    )
                    
                    self.focus_ax.fill_between(
                        x_data,
                        y_data,
                        0,
                        alpha=0.25,
                        color=color,
                        zorder=1
                    )
                    
                    self.focus_ax.plot(
                        x_data,
                        y_data,
                        color=color,
                        linewidth=3,
                        marker='o',
                        markersize=5,
                        markerfacecolor=color,
                        markeredgecolor='white',
                        markeredgewidth=1.5,
                        alpha=0.9,
                        zorder=3
                    )
                    
                    self.focus_ax.set_xlim(-1, 60)
                    self.focus_ax.set_ylim(0, 100)
                    self.focus_ax.set_xlabel("← Past  |  Time Flow (2-second intervals)  |  Now →", 
                                              color='#64748b', fontsize=10, style='italic')
                    self.focus_ax.set_ylabel("Energy Level (%)", color='#94a3b8', fontsize=11, weight='bold')
                    self.focus_ax.grid(True, alpha=0.1, color='#475569', linestyle=':')
                    self.focus_ax.tick_params(colors='#64748b', labelsize=9)
                    self.focus_ax.set_facecolor('#0a0f1e')
                    
                    self.focus_ax.spines['top'].set_visible(False)
                    self.focus_ax.spines['right'].set_visible(False)
                    self.focus_ax.spines['left'].set_color('#334155')
                    self.focus_ax.spines['bottom'].set_color('#334155')
                    self.focus_ax.spines['left'].set_linewidth(2)
                    self.focus_ax.spines['bottom'].set_linewidth(2)
                    
                    self.focus_graph_canvas.draw_idle()
                    
        except Exception as e:
            logging.error(f"Error updating focus power graph: {e}")
        
        if hasattr(self, 'root') and self.root.winfo_exists():
            self.root.after(5000, self._update_focus_power_graph)
    
    def _update_clock(self):
        if hasattr(self, 'clock_label'):
            self.clock_label.configure(text=datetime.now().strftime("%I:%M:%S %p"))
        self.root.after(1000, self._update_clock)
    
    def _update_session_stats(self):
        if not self.engine.paused:
            if self.engine.last_state in ["focus", "lowfocus"]:
                self.session_stats['focus_streak_minutes'] += 1
                if self.engine.last_state == "focus":
                    self.session_stats['deep_focus_minutes'] += 1
            else:
                self.session_stats['focus_streak_minutes'] = 0
            
            if sum(self.engine.key_rates_history[-5:]) == 0 and sum(self.engine.mouse_rates_history[-5:]) == 0:
                self.session_stats['idle_seconds'] += 60
            else:
                self.session_stats['idle_seconds'] = 0
        
        self.root.after(60000, self._update_session_stats) 
    
    def _update_state_history(self):
        """Unconditionally logs the state every 5 seconds to build the continuous grid"""
        if not self.engine.paused:
            current_state = self.engine.last_state
            current_time = datetime.now()
            
            entry = (current_time, current_state)
            self.state_history.append(entry) # Appends to the deque (max 720 blocks)
            
        if self.current_view == "history":
            self._update_history_view()
            
        self.root.after(5000, self._update_state_history)
    
    def _update_history_view(self):
        """Updates the sequential grid colors dynamically based on the last 1 hour"""
        if not hasattr(self, 'history_heatmap_blocks') or not self.history_heatmap_blocks:
            return
        
        # Only update if view is visible
        if self.current_view != "history":
            return
            
        history_list = list(self.state_history)
        
        # Batch update to reduce UI calls
        updates = []
        for i, block in enumerate(self.history_heatmap_blocks):
            if i < len(history_list):
                state = history_list[i][1]
                color = self.STATE_COLORS.get(state, self.COLORS['bg_tertiary'])
            else:
                color = self.COLORS['bg_tertiary']
            
            # Only update if color changed
            current_color = block.cget("bg")
            if current_color != color:
                updates.append((block, color))
        
        # Apply updates in batch
        for block, color in updates:
            block.configure(bg=color)
                
    def _on_closing(self):
        result = messagebox.askyesno(
            "Exit Focus App Monitor",
            "Are you sure you want to exit Focus App Monitor?\n\nYour session data will be saved.",
            icon='question'
        )
        
        if result:
            self.engine.stop()
            self.root.quit()
            self.root.destroy()
    
    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    engine = Engine()
    engine.start()
    app = EnterpriseProductivityDashboard(engine)
    app.run()
    
    