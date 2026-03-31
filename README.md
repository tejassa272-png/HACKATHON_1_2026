# 🧭 Attention Flow Cartographer

An offline, privacy-first system that maps human attention patterns using raw interaction signals. 

Unlike traditional "bossware" or productivity trackers, the Cartographer operates on a strict **zero-knowledge telemetry** protocol. It does not measure productivity, intercept keystroke content, or capture screens. Instead, it acts as an objective scientific instrument, mapping focus fragmentation and cognitive drift through physical interaction cadence.

## ✨ Key Innovations

* **Cognitive Drift Detection:** Measures micro-hesitations (drops in keystrokes and mouse velocity) in the seconds preceding an application switch to identify attention fragmentation.
* **Zero-Knowledge Architecture:** The `pynput` daemon only records interaction timestamps. Keyboard content and mouse coordinates are instantly destroyed in memory, proven by the live "Privacy Inspector" terminal.
* **Fully Offline Inference:** Uses a local Machine Learning model (`focus_rf_model.pkl`) to cluster continuous interaction vectors into distinct states (Sustained, Fragmented, Observation, Idle) with zero cloud connectivity.
* **Continuous Session Heatmap:** A custom Tkinter + Matplotlib engine renders a live, 60-minute rolling heatmap of attention states at 5-second granularity.
* **Non-Blocking Storage:** Implements an asynchronous SQLite database using WAL (Write-Ahead Logging) mode to batch-write telemetry without interrupting the UI thread.

## 🚀 Installation & Setup

### Prerequisites
* Python 3.9+
* Windows OS (Required for `pycaw` audio detection and `pygetwindow` window tracking)

### Quick Start
1. **Clone the repository:**
   ```bash
   git clone [https://github.com/tejassa272-png/HACKATHON_1_2026.git](https://github.com/tejassa272-png/HACKATHON_1_2026.git)
   cd HACKATHON_1_2026
