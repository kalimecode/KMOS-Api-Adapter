#!/usr/bin/env python3
"""
=============================================================================
УНИВЕРСАЛЬНЫЙ API-АДАПТЕР ДЛЯ КОМПЛЕКСА МОНИТОРИНГА ОКРУЖАЮЩЕЙ СРЕДЫ
=============================================================================
Слушает 0.0.0.0:81, отдаёт данные датчиков в JSON и статические файлы
Включает веб-дашборд с автообновлением

Эндпоинты:
  /                — Веб-дашборд (HTML)
  /api/getAll      — все данные в одном JSON
  /api/getBGE      — данные БГЕ (Modbus)
  /api/getSensors  — температурные датчики
  /api/camera      — информация о камере + статус
  /api/status      — статус всех источников
  /camera/*        — статические файлы (изображения камеры)

Логирование: /etc/zabbix/scripts/api_log.log (ротация 10×10MB)
Формат даты: ДД.ММ.ГГГГ ЧЧ:ММ:СС
=============================================================================
"""

# =============================================================================
# ИМПОРТЫ
# =============================================================================

import json
import os
import time
import logging
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# Импорт конфигурации
import api_config as cfg

# Модуль Modbus
try:
    from pymodbus.client import ModbusTcpClient
    MODBUS_AVAILABLE = True
except ImportError:
    MODBUS_AVAILABLE = False

# =============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# =============================================================================

def setup_logging():
    """Настройка ротации логов"""
    logger = logging.getLogger("api_adapter")
    logger.setLevel(getattr(logging, cfg.LOG_LEVEL, logging.INFO))

    handler = RotatingFileHandler(
        cfg.LOG_FILE,
        maxBytes=cfg.LOG_MAX_BYTES,
        backupCount=cfg.LOG_BACKUP_COUNT,
        encoding='utf-8'
    )

    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%d.%m.%Y %H:%M:%S'
    )
    handler.setFormatter(formatter)

    logger.addHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger

log = setup_logging()

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def now_formatted():
    """Текущее время в формате: ДД.ММ.ГГГГ ЧЧ:ММ:СС"""
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")

def now_iso():
    """Текущее время в ISO-формате"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def get_file_age_seconds(filepath):
    """Возвращает возраст файла в секундах"""
    try:
        if not os.path.exists(filepath):
            return None
        mtime = os.path.getmtime(filepath)
        age = time.time() - mtime
        return int(age)
    except:
        return None

# =============================================================================
# ФУНКЦИИ ДЛЯ ТЕМПЕРАТУРНЫХ ДАТЧИКОВ
# =============================================================================

def read_hidraw_temperature(device_path, data_length=3):
    """Читает температуру с HID-устройства"""
    try:
        if not os.path.exists(device_path):
            return {"value": None, "unit": "°C", "status": 0, "error": f"Device not found: {device_path}"}

        with open(device_path, "rb") as f:
            data = f.read(data_length)

        if len(data) < data_length:
            return {"value": None, "unit": "°C", "status": 0, "error": f"Short read: {len(data)} bytes"}

        status, low, high = data[0], data[1], data[2]

        if status != 1:
            return {"value": None, "unit": "°C", "status": 0, "error": f"Device status: {status}"}

        raw_value = (high << 8) + low
        temperature = raw_value / 100.0

        return {"value": temperature, "unit": "°C", "status": 1, "error": None}

    except PermissionError:
        return {"value": None, "unit": "°C", "status": 0, "error": f"Permission denied: {device_path}"}
    except Exception as e:
        return {"value": None, "unit": "°C", "status": 0, "error": f"{type(e).__name__}: {str(e)}"}

def get_all_temperatures():
    """Опрос всех датчиков из конфига"""
    result = {}
    for name, path in cfg.TEMP_SENSORS.items():
        log.debug(f"Reading temperature: {name} @ {path}")
        data = read_hidraw_temperature(path)
        data["device"] = path
        result[name] = data
    return result

# =============================================================================
# ФУНКЦИИ ДЛЯ БГЕ (MODBUS)
# =============================================================================

SENSOR_TYPES_BGE = {
    0: "УО-100", 1: "АМ-100", 2: "АМ-500", 3: "ХЛ-5",
    4: "ХЛ-25", 5: "ХЛ-50", 6: "СВ-30", 7: "СД-30",
    8: "О3-1", 9: "АД-10", 10: "АО-30", 11: "КС-30",
    12: "МН-2,5-Метан", 13: "МН-2,5-Пропан"
}

def decode_measure_reg(reg):
    """Декодирование регистра измерения БГЕ"""
    if reg == 0x8000:
        return -1, -1
    if reg == 0x4000:
        return -2, -1
    e = (reg >> 14) & 3
    p = (reg >> 12) & 3
    d = reg & 0xFFF
    value = d / (10 ** p)
    state = e if e < 4 else 255
    return state, value

def safe_read_modbus(client, address, count, retries=cfg.MODBUS_RETRIES):
    """Надёжное чтение регистров Modbus"""
    for attempt in range(retries):
        try:
            rr = client.read_holding_registers(address, count, slave=cfg.UNIT_ID_BGE)
            if hasattr(rr, 'registers') and rr.registers and len(rr.registers) >= count:
                return rr.registers
            raise Exception("Invalid response")
        except Exception as e:
            log.debug(f"Modbus read attempt {attempt+1}/{retries} failed: {e}")
            if attempt == retries - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
    raise Exception("Max retries exceeded")

def get_bge_data():
    """Получение данных с блока БГЕ по Modbus"""
    timestamp = now_formatted()

    if not MODBUS_AVAILABLE:
        log.warning("pymodbus not installed")
        return {"error": "pymodbus not installed", "channels": [], "timestamp": timestamp}

    client = None
    try:
        log.debug(f"Connecting to BGE @ {cfg.IP_BGE}:{cfg.PORT_BGE}")
        client = ModbusTcpClient(cfg.IP_BGE, port=cfg.PORT_BGE, timeout=cfg.MODBUS_TIMEOUT)
        client.connect()
        if not client.connected:
            raise Exception("Connection failed")

        n_channels = 1
        try:
            regs = safe_read_modbus(client, 0x0401, 1)
            n_channels = regs[0] if 1 <= regs[0] <= 16 else 1
        except:
            pass

        sensor_codes = []
        try:
            sensor_codes = safe_read_modbus(client, 0x0480, n_channels)
        except:
            sensor_codes = [255] * n_channels

        thresholds_regs = []
        try:
            thresholds_regs = safe_read_modbus(client, 0x0500, n_channels * 3)
        except:
            thresholds_regs = [0] * (n_channels * 3)

        measure_regs = []
        try:
            measure_regs = safe_read_modbus(client, 0x0000, n_channels)
        except:
            measure_regs = [0] * n_channels

        channels = []
        for i in range(n_channels):
            code = sensor_codes[i] if i < len(sensor_codes) else 255
            name = SENSOR_TYPES_BGE.get(code, "Неизвестный")
            state, value = decode_measure_reg(measure_regs[i] if i < len(measure_regs) else 0)

            base = i * 3
            p1 = p2 = p3 = 0.0
            if base + 2 < len(thresholds_regs):
                p1 = thresholds_regs[base] / 100.0
                p2 = thresholds_regs[base + 1] / 100.0
                p3 = thresholds_regs[base + 2] / 100.0

            channels.append({
                "id": i + 1,
                "sensor_code": code,
                "sensor_name": name,
                "value": value if value >= 0 else None,
                "state": state,
                "thresholds": {"p1": p1, "p2": p2, "p3": p3}
            })

        log.info(f"BGE: read {len(channels)} channels")
        return {"channels": channels, "timestamp": timestamp, "status": 1}

    except Exception as e:
        log.warning(f"BGE error: {e}")
        return {"error": str(e), "channels": [], "timestamp": timestamp, "status": 0}
    finally:
        if client:
            client.close()

# =============================================================================
# ФУНКЦИИ ДЛЯ КАМЕРЫ
# =============================================================================

def get_camera_info():
    """Информация о последнем снимке камеры"""
    timestamp = now_formatted()

    exists = os.path.exists(cfg.CAM_IMAGE)
    elapsed = get_file_age_seconds(cfg.CAM_IMAGE)

    if not exists:
        status = 0
        status_msg = "File not found"
    elif elapsed is None:
        status = 0
        status_msg = "Cannot get file age"
    elif elapsed > cfg.CAM_MAX_AGE_SECONDS:
        status = 0
        status_msg = f"Image too old ({elapsed}s > {cfg.CAM_MAX_AGE_SECONDS}s)"
    else:
        status = 1
        status_msg = "OK"

    mtime = None
    size = None

    if exists:
        try:
            stat = os.stat(cfg.CAM_IMAGE)
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M:%S")
            size = stat.st_size
        except:
            pass

    # 🔧 ИСПРАВЛЕНИЕ: возвращаем только путь, без протокола и хоста
    # Это предотвращает дублирование URL в дашборде
    camera_path = f"{cfg.CAMERA_URL_PREFIX}/{os.path.basename(cfg.CAM_IMAGE)}"

    result = {
        "url": camera_path,  # Только путь: "/camera/lastest.png"
        "file": cfg.CAM_IMAGE,
        "exists": exists,
        "modified": mtime,
        "elapsed": elapsed,
        "max_age": cfg.CAM_MAX_AGE_SECONDS,
        "status": status,
        "status_msg": status_msg,
        "size_bytes": size,
        "timestamp": timestamp
    }

    if status == 0:
        log.warning(f"Camera status FAIL: {status_msg}")
    else:
        log.debug(f"Camera status OK: elapsed={elapsed}s")

    return result

# =============================================================================
# HTML-ДАШБОРД (ШАБЛОН)
# =============================================================================

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KMOS API Adapter | Мониторинг</title>
    <style>
        :root {
            --primary-color: #3b82f6;
            --primary-dark: #2563eb;
            --success-color: #10b981;
            --warning-color: #f59e0b;
            --danger-color: #ef4444;
            --bg-color: #0f172a;
            --bg-gradient-start: #1e293b;
            --bg-gradient-end: #0f172a;
            --card-bg: #1e293b;
            --card-hover: #334155;
            --text-primary: #f1f5f9;
            --text-secondary: #94a3b8;
            --border-color: #334155;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -1px rgba(0, 0, 0, 0.2);
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, var(--bg-gradient-start) 0%, var(--bg-gradient-end) 100%);
            min-height: 100vh;
            padding: 20px;
            color: var(--text-primary);
        }

        .container { max-width: 1400px; margin: 0 auto; }

        .header {
            background: var(--card-bg);
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 24px;
            box-shadow: var(--shadow);
            text-align: center;
            border: 1px solid var(--border-color);
        }

        .header h1 {
            font-size: 2.5rem;
            font-weight: 700;
            color: var(--primary-color);
            margin-bottom: 10px;
            letter-spacing: -0.5px;
        }

        .header .subtitle {
            font-size: 1.1rem;
            color: var(--text-secondary);
            margin-bottom: 15px;
            line-height: 1.6;
        }

        .header .tagline {
            display: inline-block;
            background: linear-gradient(135deg, var(--primary-color), var(--primary-dark));
            color: white;
            padding: 8px 20px;
            border-radius: 20px;
            font-size: 0.9rem;
            font-weight: 600;
            margin-bottom: 15px;
        }

        .header .status-bar {
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-top: 20px;
            flex-wrap: wrap;
        }

        .status-item {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.95rem;
            color: var(--text-secondary);
        }

        .status-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--success-color);
            animation: pulse 2s infinite;
        }

        .status-dot.warning { background: var(--warning-color); }
        .status-dot.danger { background: var(--danger-color); }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 24px;
            margin-bottom: 24px;
        }

        @media (max-width: 768px) {
            .grid { grid-template-columns: 1fr; }
        }

        .card {
            background: var(--card-bg);
            border-radius: 16px;
            padding: 24px;
            box-shadow: var(--shadow);
            transition: transform 0.2s ease, box-shadow 0.2s ease;
            border: 1px solid var(--border-color);
        }

        .card:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3);
            background: var(--card-hover);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid var(--border-color);
        }

        .card-title {
            font-size: 1.3rem;
            font-weight: 600;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .card-icon { font-size: 1.5rem; }

        .card-status {
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.85rem;
            font-weight: 600;
        }

        .card-status.ok { background: #064e3b; color: #6ee7b7; }
        .card-status.fail { background: #7f1d1d; color: #fca5a5; }

        .data-row {
            display: flex;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid var(--border-color);
        }

        .data-row:last-child { border-bottom: none; }

        .data-label { color: var(--text-secondary); font-weight: 500; }
        .data-value { font-weight: 600; color: var(--text-primary); }
        .data-value.highlight { color: var(--primary-color); font-size: 1.1rem; }
        .data-value.success { color: var(--success-color); }
        .data-value.warning { color: var(--warning-color); }
        .data-value.danger { color: var(--danger-color); }

        .sensor-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }

        .sensor-table th {
            text-align: left;
            padding: 12px 8px;
            background: #334155;
            font-weight: 600;
            font-size: 0.85rem;
            color: var(--text-secondary);
            border-radius: 8px 8px 0 0;
        }

        .sensor-table td {
            padding: 12px 8px;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.9rem;
        }

        .sensor-table tr:last-child td { border-bottom: none; }
        .sensor-table tr:hover { background: #334155; }

        .camera-container { text-align: center; }

        .camera-image {
            width: 100%;
            max-height: 400px;
            object-fit: contain;
            border-radius: 12px;
            background: #334155;
            margin-bottom: 15px;
            border: 1px solid var(--border-color);
        }

        .camera-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }

        .camera-stat {
            background: #334155;
            padding: 12px;
            border-radius: 8px;
            text-align: center;
        }

        .camera-stat-label {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-bottom: 5px;
        }

        .camera-stat-value {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--text-primary);
        }

        .footer {
            background: var(--card-bg);
            border-radius: 16px;
            padding: 20px 30px;
            text-align: center;
            box-shadow: var(--shadow);
            border: 1px solid var(--border-color);
        }

        .footer-text { color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 8px; }
        .footer-copyright { color: var(--text-primary); font-weight: 600; font-size: 0.95rem; }
        .footer-refresh { margin-top: 10px; font-size: 0.85rem; color: var(--text-secondary); }

        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid var(--border-color);
            border-top-color: var(--primary-color);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }

        @keyframes spin { to { transform: rotate(360deg); } }

        @media (max-width: 640px) {
            .header h1 { font-size: 1.8rem; }
            .grid { grid-template-columns: 1fr; }
            .sensor-table { font-size: 0.8rem; }
            .sensor-table th, .sensor-table td { padding: 8px 4px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header class="header">
            <span class="tagline">🚀 Промышленный мониторинг нового поколения</span>
            <h1>KMOS API Adapter</h1>
            <p class="subtitle">
                Универсальная платформа сбора и визуализации данных с промышленных датчиков,<br>
                систем безопасности и видеокамер в реальном времени
            </p>
            <div class="status-bar">
                <div class="status-item">
                    <span class="status-dot" id="system-status-dot"></span>
                    <span id="system-status-text">Загрузка...</span>
                </div>
                <div class="status-item">
                    <span>🕐</span>
                    <span id="last-update">--:--:--</span>
                </div>
                <div class="status-item">
                    <span>🔄</span>
                    <span>Обновление: 30 сек</span>
                </div>
            </div>
        </header>

        <div class="grid">
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <span class="card-icon">🌡️</span>
                        Температурные датчики
                    </div>
                    <span class="card-status ok" id="sensors-status">OK</span>
                </div>
                <div id="sensors-content">
                    <div style="text-align: center; padding: 40px;">
                        <span class="loading"></span>
                        <p style="margin-top: 15px; color: var(--text-secondary);">Загрузка данных...</p>
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <span class="card-icon">🏭</span>
                        Блок БГЕ (Modbus)
                    </div>
                    <span class="card-status ok" id="bge-status">OK</span>
                </div>
                <div id="bge-content">
                    <div style="text-align: center; padding: 40px;">
                        <span class="loading"></span>
                        <p style="margin-top: 15px; color: var(--text-secondary);">Загрузка данных...</p>
                    </div>
                </div>
            </div>

            <div class="card" style="grid-column: 1 / -1;">
                <div class="card-header">
                    <div class="card-title">
                        <span class="card-icon">📷</span>
                        Видеокамера
                    </div>
                    <span class="card-status ok" id="camera-status">OK</span>
                </div>
                <div class="camera-container">
                    <img src="" alt="Камера" class="camera-image" id="camera-img" style="display: none;">
                    <div id="camera-placeholder" style="padding: 60px; background: #334155; border-radius: 12px;">
                        <span class="loading"></span>
                        <p style="margin-top: 15px; color: var(--text-secondary);">Загрузка изображения...</p>
                    </div>
                    <div class="camera-info" id="camera-info" style="display: none;">
                        <div class="camera-stat">
                            <div class="camera-stat-label">Возраст снимка</div>
                            <div class="camera-stat-value" id="camera-age">-- сек</div>
                        </div>
                        <div class="camera-stat">
                            <div class="camera-stat-label">Размер файла</div>
                            <div class="camera-stat-value" id="camera-size">-- KB</div>
                        </div>
                        <div class="camera-stat">
                            <div class="camera-stat-label">Последнее обновление</div>
                            <div class="camera-stat-value" id="camera-modified">--:--:--</div>
                        </div>
                        <div class="camera-stat">
                            <div class="camera-stat-label">Статус</div>
                            <div class="camera-stat-value" id="camera-stat-status">--</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <footer class="footer">
            <p class="footer-text">Система мониторинга и сбора данных</p>
            <p class="footer-copyright">© 2026 ООО "Цифровые Виртуальные Ассистенты"</p>
            <p class="footer-refresh">
                Последнее обновление: <span id="footer-update-time">--:--:--</span> |
                Автообновление: <span id="countdown">30</span> сек
            </p>
        </footer>
    </div>

    <script>
        const API_BASE = window.location.origin;
        const REFRESH_INTERVAL = 30;

        function formatNumber(num, decimals = 1) {
            if (num === null || num === undefined) return '--';
            return Number(num).toFixed(decimals);
        }

        function formatFileSize(bytes) {
            if (!bytes) return '-- KB';
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
        }

        function formatTime(dateStr) {
            if (!dateStr) return '--:--:--';
            const parts = dateStr.split(' ');
            return parts[1] || parts[0] || '--:--:--';
        }

        let countdown = REFRESH_INTERVAL;
        function startCountdown() {
            setInterval(() => {
                countdown--;
                document.getElementById('countdown').textContent = countdown;
                if (countdown <= 0) countdown = REFRESH_INTERVAL;
            }, 1000);
        }

        async function fetchData() {
            try {
                const response = await fetch(`${API_BASE}/api/getAll`);
                if (!response.ok) throw new Error('API error');
                const data = await response.json();

                const allOk = data.bge?.status === 1 &&
                              data.camera?.status === 1 &&
                              Object.values(data.sensors || {}).every(s => s.status === 1);

                const statusDot = document.getElementById('system-status-dot');
                const statusText = document.getElementById('system-status-text');

                if (allOk) {
                    statusDot.className = 'status-dot';
                    statusText.textContent = 'Все системы в норме';
                } else {
                    statusDot.className = 'status-dot warning';
                    statusText.textContent = 'Внимание: есть проблемы';
                }

                const now = new Date();
                const timeStr = now.toLocaleTimeString('ru-RU');
                document.getElementById('last-update').textContent = timeStr;
                document.getElementById('footer-update-time').textContent = timeStr;

                updateSensors(data.sensors);
                updateBGE(data.bge);
                updateCamera(data.camera);

            } catch (error) {
                console.error('Ошибка получения данных:', error);
                document.getElementById('system-status-dot').className = 'status-dot danger';
                document.getElementById('system-status-text').textContent = 'Ошибка подключения к API';
            }
        }

        function updateSensors(sensors) {
            const container = document.getElementById('sensors-content');
            const statusEl = document.getElementById('sensors-status');

            if (!sensors || Object.keys(sensors).length === 0) {
                container.innerHTML = '<p style="text-align: center; color: var(--text-secondary);">Нет данных</p>';
                statusEl.className = 'card-status fail';
                statusEl.textContent = 'Нет данных';
                return;
            }

            let allOk = true;
            let html = '';

            for (const [name, data] of Object.entries(sensors)) {
                const status = data.status === 1;
                if (!status) allOk = false;
                const valueClass = status ? 'success' : 'danger';
                const value = data.value !== null ? `${formatNumber(data.value)} ${data.unit || '°C'}` : '--';
                html += `<div class="data-row"><span class="data-label">${name}</span><span class="data-value ${valueClass}">${value}</span></div>`;
            }

            container.innerHTML = html;
            statusEl.className = `card-status ${allOk ? 'ok' : 'fail'}`;
            statusEl.textContent = allOk ? 'OK' : 'Внимание';
        }

        function updateBGE(bge) {
            const container = document.getElementById('bge-content');
            const statusEl = document.getElementById('bge-status');

            if (!bge || !bge.channels || bge.channels.length === 0) {
                container.innerHTML = '<p style="text-align: center; color: var(--text-secondary);">Нет данных</p>';
                statusEl.className = 'card-status fail';
                statusEl.textContent = 'Нет данных';
                return;
            }

            const status = bge.status === 1;
            statusEl.className = `card-status ${status ? 'ok' : 'fail'}`;
            statusEl.textContent = status ? 'OK' : 'Ошибка';

            let html = `<table class="sensor-table"><thead><tr><th>Канал</th><th>Датчик</th><th>Значение</th><th>Статус</th><th>Пороги (П1/П2/П3)</th></tr></thead><tbody>`;

            for (const ch of bge.channels) {
                const valueClass = ch.state === 0 ? 'success' : (ch.state === 1 ? 'warning' : 'danger');
                const value = ch.value !== null ? formatNumber(ch.value) : '--';
                const stateText = ch.state === 0 ? 'Норма' : (ch.state === 1 ? 'Предупреждение' : 'Авария');
                html += `<tr><td>${ch.id}</td><td>${ch.sensor_name}</td><td><span class="data-value ${valueClass}">${value}</span></td><td>${stateText}</td><td style="color: var(--text-secondary);">${ch.thresholds.p1} / ${ch.thresholds.p2} / ${ch.thresholds.p3}</td></tr>`;
            }

            html += '</tbody></table>';
            container.innerHTML = html;
        }

        function updateCamera(camera) {
            const imgEl = document.getElementById('camera-img');
            const placeholderEl = document.getElementById('camera-placeholder');
            const infoEl = document.getElementById('camera-info');
            const statusEl = document.getElementById('camera-status');

            if (!camera || !camera.exists) {
                placeholderEl.innerHTML = '<p style="color: var(--danger-color);">Изображение недоступно</p>';
                statusEl.className = 'card-status fail';
                statusEl.textContent = 'Нет сигнала';
                return;
            }

            const status = camera.status === 1;
            statusEl.className = `card-status ${status ? 'ok' : 'fail'}`;
            statusEl.textContent = status ? 'OK' : 'Внимание';

            // 🔧 ИСПРАВЛЕНИЕ: корректная обработка URL камеры
            // Если camera.url — полный URL, извлекаем только путь
            let cameraPath = camera.url;
            if (camera.url && camera.url.startsWith('http')) {
                try {
                    const urlObj = new URL(camera.url);
                    cameraPath = urlObj.pathname;
                } catch(e) {
                    cameraPath = camera.url;
                }
            }

            // Добавляем timestamp для bypass кэша браузера
            const timestamp = new Date().getTime();
            imgEl.src = `${API_BASE}${cameraPath}?t=${timestamp}`;
            imgEl.style.display = 'block';
            placeholderEl.style.display = 'none';
            infoEl.style.display = 'grid';

            document.getElementById('camera-age').textContent = `${camera.elapsed || '--'} сек`;
            document.getElementById('camera-size').textContent = formatFileSize(camera.size_bytes);
            document.getElementById('camera-modified').textContent = formatTime(camera.modified);
            document.getElementById('camera-stat-status').textContent = camera.status_msg || '--';

            const ageEl = document.getElementById('camera-age');
            if (camera.elapsed > camera.max_age) {
                ageEl.style.color = 'var(--danger-color)';
            } else if (camera.elapsed > camera.max_age / 2) {
                ageEl.style.color = 'var(--warning-color)';
            } else {
                ageEl.style.color = 'var(--success-color)';
            }
        }

        document.addEventListener('DOMContentLoaded', () => {
            startCountdown();
            fetchData();
            setInterval(fetchData, REFRESH_INTERVAL * 1000);
        });
    </script>
</body>
</html>'''

# =============================================================================
# HTTP-ОБРАБОТЧИК
# =============================================================================

class APIHandler(BaseHTTPRequestHandler):
    """Обработчик HTTP-запросов для API"""

    def log_message(self, format, *args):
        """Перенаправляем логи в RotatingFileHandler"""
        log.info(f"{self.address_string()} - {format % args}")

    def send_json(self, data, status=200):
        """Отправка JSON-ответа"""
        body = json.dumps(data, indent=cfg.JSON_INDENT, ensure_ascii=False).encode(cfg.JSON_ENCODING)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        """Отправка HTML-ответа"""
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_static_file(self, file_path):
        """Отправка статического файла"""
        try:
            with open(file_path, 'rb') as f:
                content = f.read()

            if file_path.lower().endswith('.png'):
                content_type = 'image/png'
            elif file_path.lower().endswith(('.jpg', '.jpeg')):
                content_type = 'image/jpeg'
            elif file_path.lower().endswith('.gif'):
                content_type = 'image/gif'
            else:
                content_type = 'application/octet-stream'

            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(content)
            log.debug(f"Sent static file: {file_path} ({len(content)} bytes)")
            return True
        except Exception as e:
            log.error(f"Error sending file {file_path}: {e}")
            return False

    def do_GET(self):
        """Обработка GET-запросов"""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'

        log.info(f"GET {path}")

        # Обработка статики (камера)
        if path.startswith(cfg.CAMERA_URL_PREFIX):
            relative_path = path[len(cfg.CAMERA_URL_PREFIX):].lstrip('/')

            if not relative_path:
                self.send_json({"error": "File name required. Example: /camera/lastest.png"}, 400)
                return

            file_path = os.path.normpath(os.path.join(cfg.CAMERA_STATIC_DIR, relative_path))
            static_dir = os.path.normpath(cfg.CAMERA_STATIC_DIR)

            if not file_path.startswith(static_dir):
                log.warning(f"Access denied to path traversal attempt: {path}")
                self.send_json({"error": "Access denied"}, 403)
                return

            if os.path.exists(file_path) and os.path.isfile(file_path):
                if not self.send_static_file(file_path):
                    self.send_json({"error": "File read error"}, 500)
            else:
                log.warning(f"Static file not found: {relative_path}")
                self.send_json({"error": "File not found", "path": relative_path}, 404)
            return

        try:
            # Главная страница — ДАШБОРД
            if path == '/' or path == '/index.html':
                log.info("Serving dashboard")
                self.send_html(DASHBOARD_HTML)
                return

            # API эндпоинты
            if path == '/api' or path == '/api/':
                self.send_json({
                    "info": "API Adapter v1.0",
                    "timestamp": now_formatted(),
                    "endpoints": {
                        "/": "Web Dashboard (HTML)",
                        "/api/getAll": "All data in one JSON",
                        "/api/getBGE": "Environmental sensors (Modbus)",
                        "/api/getSensors": "Temperature sensors (HID)",
                        "/api/camera": "Camera image info + status",
                        "/api/status": "System status",
                        "/camera/lastest.png": "Camera image (static)"
                    }
                })

            elif path == '/api/getAll':
                data = {
                    "timestamp": now_formatted(),
                    "bge": get_bge_data(),
                    "sensors": get_all_temperatures(),
                    "camera": get_camera_info()
                }
                self.send_json(data)

            elif path == '/api/getBGE':
                self.send_json(get_bge_data())

            elif path == '/api/getSensors':
                data = {
                    "timestamp": now_formatted(),
                    "sensors": get_all_temperatures()
                }
                self.send_json(data)

            elif path == '/api/camera':
                self.send_json(get_camera_info())

            elif path == '/api/status':
                status_data = {
                    "timestamp": now_formatted(),
                    "ok": True,
                    "sources": {
                        "bge": {"available": MODBUS_AVAILABLE, "host": cfg.IP_BGE, "port": cfg.PORT_BGE},
                        "camera": {"file": cfg.CAM_IMAGE, "exists": os.path.exists(cfg.CAM_IMAGE)},
                        "temperature_sensors": list(cfg.TEMP_SENSORS.keys())
                    }
                }

                if MODBUS_AVAILABLE:
                    try:
                        client = ModbusTcpClient(cfg.IP_BGE, port=cfg.PORT_BGE, timeout=cfg.MODBUS_HEALTH_TIMEOUT)
                        client.connect()
                        status_data["sources"]["bge"]["reachable"] = client.connected
                        client.close()
                    except:
                        status_data["sources"]["bge"]["reachable"] = False
                        status_data["ok"] = False

                cam_info = get_camera_info()
                status_data["sources"]["camera"]["status"] = cam_info["status"]
                if cam_info["status"] == 0:
                    status_data["ok"] = False

                sensors_ok = True
                for name, path in cfg.TEMP_SENSORS.items():
                    if not os.path.exists(path):
                        sensors_ok = False
                        break
                status_data["sources"]["temperature_sensors_ok"] = sensors_ok
                if not sensors_ok:
                    status_data["ok"] = False

                self.send_json(status_data)

            else:
                self.send_json({"error": "Not found", "path": path}, 404)

        except Exception as e:
            log.error(f"Handler error: {e}")
            self.send_json({"error": f"Internal error: {str(e)}"}, 500)

# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

def main():
    log.info("=" * 60)
    log.info("Starting API Adapter with Dashboard")
    log.info(f"Listening on {cfg.API_HOST}:{cfg.API_PORT}")
    log.info(f"Dashboard: http://{cfg.API_HOST}:{cfg.API_PORT}/")
    log.info(f"Config: BGE={cfg.IP_BGE}:{cfg.PORT_BGE}, Camera={cfg.CAM_IMAGE}")
    log.info(f"Temperature sensors: {list(cfg.TEMP_SENSORS.keys())}")
    log.info(f"Log file: {cfg.LOG_FILE} (max {cfg.LOG_BACKUP_COUNT} × {cfg.LOG_MAX_BYTES // 1024 // 1024}MB)")
    log.info("=" * 60)

    server = HTTPServer((cfg.API_HOST, cfg.API_PORT), APIHandler)
    server.socket.settimeout(None)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down by user request")
    finally:
        server.server_close()
        log.info("API Adapter stopped")

if __name__ == "__main__":
    main()
