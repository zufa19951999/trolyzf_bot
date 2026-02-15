import os
import sys
import threading
import time
import requests
import json
import sqlite3
import logging
import shutil
import re
import csv
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.error import TelegramError
from functools import wraps

# ==================== THIẾT LẬP LOGGING CHI TIẾT ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==================== THỜI GIAN VIỆT NAM & BẢO MẬT ====================

def get_vn_time():
    return datetime.utcnow() + timedelta(hours=7)

def format_vn_time():
    return get_vn_time().strftime("%H:%M:%S %d/%m/%Y")

def format_vn_time_short():
    return get_vn_time().strftime("%H:%M %d/%m")

class SecurityManager:
    def __init__(self):
        self.rate_limits = {}
        self.max_requests_per_minute = 30
    
    def sanitize_input(self, text):
        dangerous = [';', '--', 'DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'EXEC', 'UNION']
        text_upper = text.upper()
        for item in dangerous:
            if item in text_upper:
                text = text.replace(item, '')
        return text.strip()

security = SecurityManager()

def rate_limit(max_calls=30):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            current_time = time.time()
            
            if user_id in security.rate_limits:
                calls, first_call = security.rate_limits[user_id]
                if current_time - first_call < 60:
                    if calls >= max_calls:
                        await update.message.reply_text(
                            f"⚠️ Bạn đã gửi quá nhiều request. Vui lòng thử lại sau 1 phút.\n\n🕐 {format_vn_time()}"
                        )
                        return
                    security.rate_limits[user_id] = (calls + 1, first_call)
                else:
                    security.rate_limits[user_id] = (1, current_time)
            else:
                security.rate_limits[user_id] = (1, current_time)
            
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# ==================== BẮT LỖI KHỞI ĐỘNG ====================
try:
    load_dotenv()

    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    CMC_API_KEY = os.getenv('CMC_API_KEY')
    CMC_API_URL = "https://pro-api.coinmarketcap.com/v1"

    if not TELEGRAM_TOKEN:
        logger.error("❌ THIẾU TELEGRAM_TOKEN")
        raise ValueError("TELEGRAM_TOKEN không được để trống")
    
    if not CMC_API_KEY:
        logger.warning("⚠️ THIẾU CMC_API_KEY - Một số chức năng sẽ không hoạt động")

    # ==================== CẤU HÌNH DATABASE TRÊN RENDER DISK ====================
    DATA_DIR = '/data' if os.path.exists('/data') else os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(DATA_DIR, 'crypto_bot.db')
    BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
    EXPORT_DIR = os.path.join(DATA_DIR, 'exports')

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    logger.info(f"📁 Dữ liệu sẽ được lưu tại: {DB_PATH}")

    # Cache
    price_cache = {}
    usdt_cache = {'rate': None, 'time': None}

    # Biến toàn cục cho bot
    app = None

    # ==================== HEALTH CHECK SERVER CHO RENDER ====================
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            
            current_time = format_vn_time()
            response = f"Crypto Bot Running - {current_time}"
            self.wfile.write(response.encode('utf-8'))
        
        def log_message(self, format, *args):
            return

    def run_health_server():
        try:
            port = int(os.environ.get('PORT', 10000))
            server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
            logger.info(f"✅ Health server running on port {port}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"❌ Health server error: {e}")
            time.sleep(10)

    # ==================== DATABASE SETUP ====================
    def init_database():
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS portfolio
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER,
                          symbol TEXT,
                          amount REAL,
                          buy_price REAL,
                          buy_date TEXT,
                          total_cost REAL)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS alerts
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER,
                          symbol TEXT,
                          target_price REAL,
                          condition TEXT,
                          is_active INTEGER DEFAULT 1,
                          created_at TEXT,
                          triggered_at TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS expense_categories
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER,
                          name TEXT,
                          budget REAL,
                          created_at TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS expenses
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER,
                          category_id INTEGER,
                          amount REAL,
                          currency TEXT DEFAULT 'VND',
                          note TEXT,
                          expense_date TEXT,
                          created_at TEXT,
                          FOREIGN KEY (category_id) REFERENCES expense_categories(id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS incomes
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER,
                          amount REAL,
                          currency TEXT DEFAULT 'VND',
                          source TEXT,
                          income_date TEXT,
                          note TEXT,
                          created_at TEXT)''')
            
            conn.commit()
            logger.info(f"✅ Database initialized at {DB_PATH}")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi khởi tạo database: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def migrate_database():
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            c = conn.cursor()
            
            c.execute("PRAGMA table_info(incomes)")
            columns = [column[1] for column in c.fetchall()]
            if 'currency' not in columns:
                c.execute("ALTER TABLE incomes ADD COLUMN currency TEXT DEFAULT 'VND'")
            
            c.execute("PRAGMA table_info(expenses)")
            columns = [column[1] for column in c.fetchall()]
            if 'currency' not in columns:
                c.execute("ALTER TABLE expenses ADD COLUMN currency TEXT DEFAULT 'VND'")
                
            conn.commit()
        except Exception as e:
            logger.error(f"❌ Lỗi khi migrate database: {e}")
        finally:
            if conn:
                conn.close()
                
    def backup_database():
        try:
            if os.path.exists(DB_PATH):
                timestamp = get_vn_time().strftime('%Y%m%d_%H%M%S')
                backup_path = os.path.join(BACKUP_DIR, f'backup_{timestamp}.db')
                shutil.copy2(DB_PATH, backup_path)
                clean_old_backups()
        except Exception as e:
            logger.error(f"❌ Lỗi backup: {e}")

    def clean_old_backups(days=7):
        try:
            now = time.time()
            for f in os.listdir(BACKUP_DIR):
                if f.startswith('backup_') and f.endswith('.db'):
                    filepath = os.path.join(BACKUP_DIR, f)
                    if os.path.getmtime(filepath) < now - days * 86400:
                        os.remove(filepath)
        except Exception as e:
            logger.error(f"Lỗi clean old backups: {e}")

    def clean_old_exports(hours=24):
        try:
            now = time.time()
            for f in os.listdir(EXPORT_DIR):
                if f.startswith('portfolio_') and f.endswith('.csv'):
                    filepath = os.path.join(EXPORT_DIR, f)
                    if os.path.getmtime(filepath) < now - hours * 3600:
                        os.remove(filepath)
        except Exception as e:
            logger.error(f"Lỗi clean old exports: {e}")

    def schedule_cleanup():
        while True:
            try:
                clean_old_exports()
                time.sleep(21600)
            except:
                time.sleep(3600)

    def schedule_backup():
        while True:
            try:
                backup_database()
                time.sleep(86400)
            except:
                time.sleep(3600)

    # ==================== PORTFOLIO DATABASE FUNCTIONS ====================
    def add_transaction(user_id, symbol, amount, buy_price):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            buy_date = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            total_cost = amount * buy_price
            symbol_upper = symbol.upper()
            
            c.execute('''INSERT INTO portfolio 
                         (user_id, symbol, amount, buy_price, buy_date, total_cost)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (user_id, symbol_upper, amount, buy_price, buy_date, total_cost))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi khi thêm transaction: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_portfolio(user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT symbol, amount, buy_price, buy_date, total_cost 
                         FROM portfolio WHERE user_id = ? ORDER BY buy_date''',
                      (user_id,))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi khi lấy portfolio: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_transaction_detail(user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT id, symbol, amount, buy_price, buy_date, total_cost 
                         FROM portfolio WHERE user_id = ? ORDER BY buy_date''',
                      (user_id,))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi khi lấy transaction detail: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def delete_transaction(transaction_id, user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''DELETE FROM portfolio 
                         WHERE id = ? AND user_id = ?''',
                      (transaction_id, user_id))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Lỗi khi xóa transaction: {e}")
            return False
        finally:
            if conn:
                conn.close()

    # ==================== ALERTS FUNCTIONS ====================
    def add_alert(user_id, symbol, target_price, condition):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            symbol_upper = symbol.upper()
            
            c.execute('''INSERT INTO alerts 
                         (user_id, symbol, target_price, condition, created_at)
                         VALUES (?, ?, ?, ?, ?)''',
                      (user_id, symbol_upper, target_price, condition, created_at))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi thêm alert: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_user_alerts(user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT id, symbol, target_price, condition, created_at 
                         FROM alerts 
                         WHERE user_id = ? AND is_active = 1 
                         ORDER BY created_at''', (user_id,))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi lấy alerts: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def delete_alert(alert_id, user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Lỗi xóa alert: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def check_alerts():
        global app
        while True:
            try:
                time.sleep(60)
                
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT id, user_id, symbol, target_price, condition 
                             FROM alerts WHERE is_active = 1''')
                alerts = c.fetchall()
                conn.close()
                
                for alert in alerts:
                    alert_id, user_id, symbol, target_price, condition = alert
                    price_data = get_price(symbol)
                    if not price_data:
                        continue
                    
                    current_price = price_data['p']
                    should_trigger = False
                    
                    if condition == 'above' and current_price >= target_price:
                        should_trigger = True
                    elif condition == 'below' and current_price <= target_price:
                        should_trigger = True
                    
                    if should_trigger and app:
                        msg = (
                            f"🔔 *CẢNH BÁO GIÁ*\n━━━━━━━━━━━━━━━━\n\n"
                            f"• Coin: *{symbol}*\n"
                            f"• Giá hiện tại: `{fmt_price(current_price)}`\n"
                            f"• Mốc cảnh báo: `{fmt_price(target_price)}`\n"
                            f"• Điều kiện: {'📈 Lên trên' if condition == 'above' else '📉 Xuống dưới'}\n\n"
                            f"🕐 {format_vn_time()}"
                        )
                        
                        try:
                            app.bot.send_message(user_id, msg, parse_mode='Markdown')
                            
                            conn = sqlite3.connect(DB_PATH)
                            c = conn.cursor()
                            c.execute('''UPDATE alerts SET is_active = 0, triggered_at = ? 
                                         WHERE id = ?''', 
                                      (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), alert_id))
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            logger.error(f"❌ Lỗi gửi alert {alert_id}: {e}")
            except Exception as e:
                logger.error(f"❌ Lỗi check_alerts: {e}")
                time.sleep(10)

    # ==================== HÀM LẤY GIÁ COIN ====================
    def get_price(symbol):
        try:
            if not CMC_API_KEY:
                return None
                
            clean_symbol = symbol.upper()
            if clean_symbol == 'USDT':
                clean = 'USDT'
            else:
                clean = clean_symbol.replace('USDT', '').replace('USD', '')
            
            headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY, 'Accept': 'application/json'}
            params = {'symbol': clean, 'convert': 'USD'}
            
            res = requests.get(f"{CMC_API_URL}/cryptocurrency/quotes/latest", headers=headers, params=params, timeout=10)
            
            if res.status_code == 200:
                data = res.json()
                if 'data' not in data or clean not in data['data']:
                    return None
                    
                coin_data = data['data'][clean]
                quote_data = coin_data['quote']['USD']
                
                return {
                    'p': quote_data['price'], 
                    'v': quote_data['volume_24h'], 
                    'c': quote_data['percent_change_24h'], 
                    'm': quote_data['market_cap'],
                    'n': coin_data['name'],
                    'r': coin_data.get('cmc_rank', 'N/A')
                }
            else:
                return None
        except Exception as e:
            logger.error(f"❌ Lỗi get_price {symbol}: {e}")
            return None

    def get_usdt_vnd_rate():
        global usdt_cache
        
        try:
            if usdt_cache['rate'] and usdt_cache['time']:
                time_diff = (datetime.now() - usdt_cache['time']).total_seconds()
                if time_diff < 180:
                    return usdt_cache['rate']
            
            try:
                url = "https://api.coingecko.com/api/v3/simple/price"
                params = {'ids': 'tether', 'vs_currencies': 'vnd'}
                res = requests.get(url, params=params, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    if 'tether' in data:
                        vnd_rate = float(data['tether']['vnd'])
                        result = {
                            'source': 'CoinGecko',
                            'vnd': vnd_rate,
                            'update_time': format_vn_time()
                        }
                        usdt_cache['rate'] = result
                        usdt_cache['time'] = datetime.now()
                        return result
            except:
                pass
            
            result = {
                'source': 'Fallback (25000)',
                'vnd': 25000,
                'update_time': format_vn_time()
            }
            usdt_cache['rate'] = result
            usdt_cache['time'] = datetime.now()
            return result
        except Exception as e:
            logger.error(f"❌ Lỗi get_usdt_vnd_rate: {e}")
            return {'source': 'Error', 'vnd': 25000, 'update_time': format_vn_time()}

    # ==================== HÀM ĐỊNH DẠNG ====================
    def fmt_price(p):
        try:
            p = float(p)
            if p < 0.01:
                return f"${p:.6f}"
            elif p < 1:
                return f"${p:.4f}"
            else:
                return f"${p:,.2f}"
        except: 
            return f"${p}"

    def fmt_vnd(p):
        try:
            p = float(p)
            return f"₫{p:,.0f}"
        except:
            return f"₫{p}"

    def fmt_vol(v):
        try:
            v = float(v)
            if v > 1e9:
                return f"${v/1e9:.2f}B"
            elif v > 1e6:
                return f"${v/1e6:.2f}M"
            elif v > 1e3:
                return f"${v/1e3:.2f}K"
            else:
                return f"${v:,.2f}"
        except: 
            return str(v)

    def fmt_percent(c):
        try:
            c = float(c)
            emoji = "📈" if c > 0 else "📉" if c < 0 else "➡️"
            return f"{emoji} {c:+.2f}%"
        except:
            return str(c)

    def format_currency_simple(amount, currency):
        try:
            amount = float(amount)
            if currency == 'VND':
                if amount >= 1000000:
                    return f"{amount/1000000:.1f} triệu VND"
                elif amount >= 1000:
                    return f"{amount/1000:.0f} nghìn VND"
                else:
                    return f"{amount:,.0f} VND"
            elif currency == 'USD':
                return f"${amount:,.2f}"
            else:
                return f"{amount:,.2f} {currency}"
        except:
            return f"{amount} {currency}"

    SUPPORTED_CURRENCIES = {
        'VND': '🇻🇳 Việt Nam Đồng', 'USD': '🇺🇸 US Dollar', 'USDT': '💵 Tether',
        'KHR': '🇰🇭 Riel Campuchia', 'LKR': '🇱🇰 Sri Lanka Rupee'
    }

    # ==================== EXPENSE DATABASE FUNCTIONS ====================
    def add_expense_category(user_id, name, budget=0):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute('''INSERT INTO expense_categories 
                         (user_id, name, budget, created_at)
                         VALUES (?, ?, ?, ?)''',
                      (user_id, name.upper(), budget, created_at))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi thêm category: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_expense_categories(user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT id, name, budget, created_at 
                         FROM expense_categories WHERE user_id = ? 
                         ORDER BY name''', (user_id,))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi lấy categories: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def add_income(user_id, amount, source, currency='VND', note=""):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            now = get_vn_time()
            income_date = now.strftime("%Y-%m-%d")
            created_at = now.strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute('''INSERT INTO incomes 
                         (user_id, amount, source, income_date, note, created_at, currency)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (user_id, amount, source, income_date, note, created_at, currency))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi thêm income: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def add_expense(user_id, category_id, amount, currency='VND', note=""):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            now = get_vn_time()
            expense_date = now.strftime("%Y-%m-%d")
            created_at = now.strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute('''INSERT INTO expenses 
                         (user_id, category_id, amount, note, expense_date, created_at, currency)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (user_id, category_id, amount, note, expense_date, created_at, currency))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi thêm expense: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_recent_incomes(user_id, limit=10):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT id, amount, source, note, income_date, currency
                         FROM incomes 
                         WHERE user_id = ?
                         ORDER BY income_date DESC, created_at DESC
                         LIMIT ?''', (user_id, limit))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi recent incomes: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_recent_expenses(user_id, limit=10):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT e.id, ec.name, e.amount, e.note, e.expense_date, e.currency
                         FROM expenses e
                         JOIN expense_categories ec ON e.category_id = ec.id
                         WHERE e.user_id = ?
                         ORDER BY e.expense_date DESC, e.created_at DESC
                         LIMIT ?''', (user_id, limit))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi recent expenses: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_income_by_period(user_id, period='month'):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            now = get_vn_time()
            
            if period == 'day':
                date_filter = now.strftime("%Y-%m-%d")
                query = '''SELECT source, SUM(amount), COUNT(id), currency
                          FROM incomes WHERE user_id = ? AND income_date = ?
                          GROUP BY source, currency'''
                c.execute(query, (user_id, date_filter))
            elif period == 'month':
                month_filter = now.strftime("%Y-%m")
                query = '''SELECT source, SUM(amount), COUNT(id), currency
                          FROM incomes WHERE user_id = ? AND strftime('%Y-%m', income_date) = ?
                          GROUP BY source, currency'''
                c.execute(query, (user_id, month_filter))
            else:
                year_filter = now.strftime("%Y")
                query = '''SELECT source, SUM(amount), COUNT(id), currency
                          FROM incomes WHERE user_id = ? AND strftime('%Y', income_date) = ?
                          GROUP BY source, currency'''
                c.execute(query, (user_id, year_filter))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi income summary: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_expenses_by_period(user_id, period='month'):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            now = get_vn_time()
            
            if period == 'day':
                date_filter = now.strftime("%Y-%m-%d")
                query = '''SELECT ec.name, SUM(e.amount), COUNT(e.id), ec.budget, e.currency
                          FROM expenses e
                          JOIN expense_categories ec ON e.category_id = ec.id
                          WHERE e.user_id = ? AND e.expense_date = ?
                          GROUP BY ec.name, ec.budget, e.currency'''
                c.execute(query, (user_id, date_filter))
            elif period == 'month':
                month_filter = now.strftime("%Y-%m")
                query = '''SELECT ec.name, SUM(e.amount), COUNT(e.id), ec.budget, e.currency
                          FROM expenses e
                          JOIN expense_categories ec ON e.category_id = ec.id
                          WHERE e.user_id = ? AND strftime('%Y-%m', e.expense_date) = ?
                          GROUP BY ec.name, ec.budget, e.currency'''
                c.execute(query, (user_id, month_filter))
            else:
                year_filter = now.strftime("%Y")
                query = '''SELECT ec.name, SUM(e.amount), COUNT(e.id), ec.budget, e.currency
                          FROM expenses e
                          JOIN expense_categories ec ON e.category_id = ec.id
                          WHERE e.user_id = ? AND strftime('%Y', e.expense_date) = ?
                          GROUP BY ec.name, ec.budget, e.currency'''
                c.execute(query, (user_id, year_filter))
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi expenses summary: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def delete_expense(expense_id, user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''DELETE FROM expenses WHERE id = ? AND user_id = ?''', (expense_id, user_id))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Lỗi xóa expense: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def delete_income(income_id, user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''DELETE FROM incomes WHERE id = ? AND user_id = ?''', (income_id, user_id))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Lỗi xóa income: {e}")
            return False
        finally:
            if conn:
                conn.close()

    # ==================== KEYBOARD ====================
    def get_main_keyboard():
        keyboard = [
            [KeyboardButton("💰 ĐẦU TƯ COIN"), KeyboardButton("💸 QUẢN LÝ CHI TIÊU")],
            [KeyboardButton("❓ HƯỚNG DẪN")]
        ]
        # Không dùng selective để keyboard hiển thị cho tất cả mọi người trong nhóm
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_invest_menu_keyboard():
        keyboard = [
            [InlineKeyboardButton("₿ BTC", callback_data="price_BTC"),
             InlineKeyboardButton("Ξ ETH", callback_data="price_ETH"),
             InlineKeyboardButton("Ξ SOL", callback_data="price_SOL"),
             InlineKeyboardButton("💵 USDT", callback_data="price_USDT")],
            [InlineKeyboardButton("📊 Top 10", callback_data="show_top10"),
             InlineKeyboardButton("💼 Danh mục", callback_data="show_portfolio")],
            [InlineKeyboardButton("📈 Lợi nhuận", callback_data="show_profit"),
             InlineKeyboardButton("✏️ Sửa/Xóa", callback_data="edit_transactions")],
            [InlineKeyboardButton("🔔 Cảnh báo giá", callback_data="show_alerts"),
             InlineKeyboardButton("📊 Thống kê", callback_data="show_stats")],
            [InlineKeyboardButton("📥 Xuất CSV", callback_data="export_csv"),
             InlineKeyboardButton("➖ Bán coin", callback_data="show_sell")],
            [InlineKeyboardButton("➕ Mua coin", callback_data="show_buy")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_expense_menu_keyboard():
        keyboard = [
            [InlineKeyboardButton("💰 THU NHẬP", callback_data="expense_income_menu"),
             InlineKeyboardButton("💸 CHI TIÊU", callback_data="expense_expense_menu")],
            [InlineKeyboardButton("📋 DANH MỤC", callback_data="expense_categories"),
             InlineKeyboardButton("📊 BÁO CÁO", callback_data="expense_report_menu")],
            [InlineKeyboardButton("📅 HÔM NAY", callback_data="expense_today"),
             InlineKeyboardButton("📅 THÁNG NÀY", callback_data="expense_month")],
            [InlineKeyboardButton("🔄 GẦN ĐÂY", callback_data="expense_recent"),
             InlineKeyboardButton("📥 XUẤT CSV", callback_data="expense_export")],
            [InlineKeyboardButton("🔙 VỀ MENU CHÍNH", callback_data="back_to_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

    # ==================== COMMAND HANDLERS ====================

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        welcome_msg = (
            "🚀 *ĐẦU TƯ COIN & QUẢN LÝ CHI TIÊU*\n\n"
            "🤖 Bot hỗ trợ:\n\n"
            "*💎 ĐẦU TƯ COIN:*\n"
            "• Xem giá bất kỳ coin nào\n"
            "• Top 10 coin\n"
            "• Quản lý danh mục đầu tư\n"
            "• Tính lợi nhuận chi tiết\n"
            "• Cảnh báo giá\n\n"
            "*💰 QUẢN LÝ CHI TIÊU:*\n"
            "• Ghi chép thu nhập/chi tiêu\n"
            "• Hỗ trợ đa tiền tệ\n"
            "• Quản lý ngân sách theo danh mục\n"
            "• Báo cáo theo ngày/tháng/năm\n\n"
            f"🕐 *Hiện tại:* `{format_vn_time()}`\n\n"
            "👇 *Chọn chức năng bên dưới*"
        )
        await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())
    
    async def menu_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Lệnh /menu - Hiển thị bàn phím chính cho tất cả mọi người trong nhóm"""
        await update.message.reply_text(
            "👇 *CHỌN CHỨC NĂNG* 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard()
        )
    
    async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        help_msg = (
            "📘 *HƯỚNG DẪN*\n\n"
            "*ĐẦU TƯ COIN:*\n"
            "• `/s btc eth` - Xem giá coin\n"
            "• `/usdt` - Tỷ giá USDT/VND\n"
            "• `/buy btc 0.5 40000` - Mua coin\n"
            "• `/sell btc 0.2` - Bán coin\n"
            "• `/edit` - Xem/sửa giao dịch\n"
            "• `/del [id]` - Xóa giao dịch\n"
            "• `/alert BTC above 50000` - Cảnh báo giá\n"
            "• `/alerts` - Xem cảnh báo\n"
            "• `/stats` - Thống kê danh mục\n\n"
            "*QUẢN LÝ CHI TIÊU:*\n"
            "• `tn 500000` - Thêm thu nhập\n"
            "• `dm Ăn uống 3000000` - Tạo danh mục\n"
            "• `ct 1 50000 VND Ăn trưa` - Chi tiêu\n"
            "• `ds` - Xem giao dịch gần đây\n"
            "• `bc` - Báo cáo tháng này\n"
            "• `xoa chi 5` - Xóa khoản chi\n"
            "• `xoa thu 3` - Xóa khoản thu\n\n"
            f"🕐 {format_vn_time()}"
        )
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN)
    
    # Các handler khác (usdt_command, s_command, buy_command, ...) giữ nguyên như code gốc
    # Tôi chỉ liệt kê tên để tiết kiệm không gian, bạn copy từ code cũ vào đây.
    
    @rate_limit(30)
    async def usdt_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("🔄 Đang tra cứu...")
        rate_data = get_usdt_vnd_rate()
        vnd = rate_data['vnd']
        
        text = (
            "💱 *TỶ GIÁ USDT/VND*\n━━━━━━━━━━━━━━━━\n\n"
            f"🇺🇸 *1 USDT* = `{fmt_vnd(vnd)}`\n"
            f"🇻🇳 *1,000,000 VND* = `{1000000/vnd:.4f} USDT`\n\n"
            f"⏱ *Cập nhật:* `{rate_data['update_time']}`\n"
            f"📊 *Nguồn:* `{rate_data['source']}`"
        )
        
        keyboard = [[InlineKeyboardButton("🔄 Làm mới", callback_data="refresh_usdt")],
                    [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
        
        await msg.delete()
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    
    @rate_limit(30)
    async def s_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await update.message.reply_text("❌ /s btc eth doge")
        
        msg = await update.message.reply_text("🔄 Đang tra cứu...")
        results = []
        
        for arg in ctx.args:
            symbol = arg.upper()
            d = get_price(symbol)
            
            if d:
                if symbol == 'USDT':
                    rate_data = get_usdt_vnd_rate()
                    vnd_price = rate_data['vnd']
                    results.append(f"*{d['n']}* #{d['r']}\n💰 USD: `{fmt_price(d['p'])}`\n🇻🇳 VND: `{fmt_vnd(vnd_price)}`\n📈 24h: `{d['c']:.2f}%`")
                else:
                    results.append(f"*{d['n']}* #{d['r']}\n💰 Giá: `{fmt_price(d['p'])}`\n📈 24h: `{d['c']:.2f}%`")
            else:
                results.append(f"❌ *{symbol}*: Không có dữ liệu")
        
        await msg.delete()
        await update.message.reply_text("\n━━━━━━━━━━━━\n".join(results) + f"\n\n🕐 {format_vn_time_short()}", parse_mode='Markdown')
    
    # ... (các hàm buy_command, sell_command, edit_command, delete_tx_command, alert_command, alerts_command, stats_command giữ nguyên)
    # Để code gọn, tôi sẽ không copy lại toàn bộ phần đó ở đây, bạn hãy copy từ code gốc của bạn vào vị trí tương ứng.

    # ==================== HANDLE MESSAGE ====================
    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        
        # Xử lý các lệnh tắt chi tiêu
        if text.startswith(('tn ', 'dm ', 'ct ', 'ds', 'bc', 'xoa chi ', 'xoa thu ')):
            await expense_shortcut_handler(update, ctx)
            return
        
        # Menu chính
        if text == "💰 ĐẦU TƯ COIN":
            await update.message.reply_text(
                f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_invest_menu_keyboard()
            )
        elif text == "💸 QUẢN LÝ CHI TIÊU":
            await update.message.reply_text(
                f"💰 *QUẢN LÝ CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_expense_menu_keyboard()
            )
        elif text == "❓ HƯỚNG DẪN":
            await help_command(update, ctx)
        else:
            await update.message.reply_text(
                f"❌ Không hiểu lệnh. Gõ /help để xem hướng dẫn.\n\n🕐 {format_vn_time_short()}"
            )

    # ==================== EXPENSE SHORTCUT HANDLER ====================
    async def expense_shortcut_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # (Giữ nguyên code cũ của bạn)
        text = update.message.text.strip()
        user_id = update.effective_user.id
        
        # THU NHẬP
        if text.startswith('tn '):
            parts = text.split()
            if len(parts) < 2:
                await update.message.reply_text("❌ Thiếu số tiền! VD: `tn 500000`", parse_mode=ParseMode.MARKDOWN)
                return
            
            try:
                amount = float(parts[1].replace(',', ''))
                if amount <= 0:
                    await update.message.reply_text("❌ Số tiền phải lớn hơn 0!")
                    return
                
                currency = 'VND'
                source = "Khác"
                note = ""
                
                if len(parts) >= 3:
                    if parts[2].upper() in SUPPORTED_CURRENCIES:
                        currency = parts[2].upper()
                        if len(parts) >= 4:
                            source = parts[3]
                            note = " ".join(parts[4:]) if len(parts) > 4 else ""
                    else:
                        source = parts[2]
                        note = " ".join(parts[3:]) if len(parts) > 3 else ""
                
                if add_income(user_id, amount, source, currency, note):
                    await update.message.reply_text(
                        f"✅ *ĐÃ THÊM THU NHẬP*\n━━━━━━━━━━━━━━━━\n\n"
                        f"💰 Số tiền: *{format_currency_simple(amount, currency)}*\n"
                        f"📌 Nguồn: *{source}*\n"
                        f"📝 Ghi chú: *{note if note else 'Không có'}*\n\n"
                        f"🕐 {format_vn_time()}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text("❌ Lỗi khi thêm thu nhập!")
            except ValueError:
                await update.message.reply_text("❌ Số tiền không hợp lệ!")
        
        # DANH MỤC
        elif text.startswith('dm '):
            parts = text.split()
            if len(parts) < 2:
                await update.message.reply_text("❌ Thiếu tên danh mục! VD: `dm Ăn uống 3000000`")
                return
            
            name = parts[1]
            budget = 0
            if len(parts) > 2:
                try:
                    budget = float(parts[2].replace(',', ''))
                except ValueError:
                    await update.message.reply_text("❌ Ngân sách không hợp lệ!")
                    return
            
            if add_expense_category(user_id, name, budget):
                await update.message.reply_text(
                    f"✅ *ĐÃ THÊM DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                    f"📋 Tên: *{name.upper()}*\n"
                    f"💰 Budget: {format_currency_simple(budget, 'VND')}\n\n"
                    f"🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text("❌ Lỗi khi thêm danh mục!")
        
        # CHI TIÊU
        elif text.startswith('ct '):
            parts = text.split()
            if len(parts) < 3:
                await update.message.reply_text("❌ Thiếu thông tin! VD: `ct 1 50000 VND Ăn trưa`")
                return
            
            try:
                category_id = int(parts[1])
                amount = float(parts[2].replace(',', ''))
                
                if amount <= 0:
                    await update.message.reply_text("❌ Số tiền phải lớn hơn 0!")
                    return
                
                currency = 'VND'
                start_idx = 3
                
                if len(parts) > 3 and parts[3].upper() in SUPPORTED_CURRENCIES:
                    currency = parts[3].upper()
                    start_idx = 4
                
                note = " ".join(parts[start_idx:]) if len(parts) > start_idx else ""
                
                categories = get_expense_categories(user_id)
                category_exists = False
                category_name = ""
                for cat in categories:
                    if cat[0] == category_id:
                        category_exists = True
                        category_name = cat[1]
                        break
                
                if not category_exists:
                    await update.message.reply_text(f"❌ Không tìm thấy danh mục #{category_id}!")
                    return
                
                if add_expense(user_id, category_id, amount, currency, note):
                    await update.message.reply_text(
                        f"✅ *ĐÃ THÊM CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n"
                        f"💰 Số tiền: *{format_currency_simple(amount, currency)}*\n"
                        f"📂 Danh mục: *{category_name}*\n"
                        f"📝 Ghi chú: *{note if note else 'Không có'}*\n\n"
                        f"🕐 {format_vn_time()}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text("❌ Lỗi khi thêm chi tiêu!")
            except ValueError:
                await update.message.reply_text("❌ ID hoặc số tiền không hợp lệ!")
        
        # XEM GẦN ĐÂY
        elif text == 'ds':
            recent_incomes = get_recent_incomes(user_id, 5)
            recent_expenses = get_recent_expenses(user_id, 5)
            
            if not recent_incomes and not recent_expenses:
                await update.message.reply_text("📭 Chưa có giao dịch nào!")
                return
            
            msg = "🔄 *GIAO DỊCH GẦN ĐÂY*\n━━━━━━━━━━━━━━━━\n\n"
            
            if recent_incomes:
                msg += "*💰 THU NHẬP:*\n"
                for inc in recent_incomes:
                    inc_id, amount, source, note, date, currency = inc
                    msg += f"• #{inc_id} {date}: {format_currency_simple(amount, currency)} - {source}\n"
                msg += "\n"
            
            if recent_expenses:
                msg += "*💸 CHI TIÊU:*\n"
                for exp in recent_expenses:
                    exp_id, cat_name, amount, note, date, currency = exp
                    msg += f"• #{exp_id} {date}: {format_currency_simple(amount, currency)} - {cat_name}\n"
            
            msg += f"\n🕐 {format_vn_time()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        
        # BÁO CÁO NHANH
        elif text == 'bc':
            expenses = get_expenses_by_period(user_id, 'month')
            incomes = get_income_by_period(user_id, 'month')
            
            msg = f"📊 *BÁO CÁO THÁNG {get_vn_time().strftime('%m/%Y')}*\n━━━━━━━━━━━━━━━━\n\n"
            
            if incomes:
                total_income = 0
                msg += "*💰 THU NHẬP:*\n"
                for inc in incomes:
                    source, amount, count, currency = inc
                    total_income += amount
                    msg += f"• {source}: {format_currency_simple(amount, currency)} ({count} lần)\n"
                msg += f"\n• *Tổng thu:* {format_currency_simple(total_income, 'VND')}\n\n"
            else:
                msg += "📭 Chưa có thu nhập.\n\n"
            
            if expenses:
                total_expense = 0
                msg += "*💸 CHI TIÊU:*\n"
                for exp in expenses:
                    cat_name, amount, count, budget, currency = exp
                    total_expense += amount
                    msg += f"• {cat_name}: {format_currency_simple(amount, currency)} ({count} lần)\n"
                msg += f"\n• *Tổng chi:* {format_currency_simple(total_expense, 'VND')}\n"
            else:
                msg += "📭 Chưa có chi tiêu."
            
            msg += f"\n\n🕐 {format_vn_time()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        
        # XÓA CHI TIÊU
        elif text.startswith('xoa chi '):
            parts = text.split()
            if len(parts) < 3:
                await update.message.reply_text("❌ Cần có ID! VD: `xoa chi 5`")
                return
            
            try:
                expense_id = int(parts[2])
                if delete_expense(expense_id, user_id):
                    await update.message.reply_text(f"✅ Đã xóa khoản chi #{expense_id}\n\n🕐 {format_vn_time_short()}")
                else:
                    await update.message.reply_text(f"❌ Không tìm thấy khoản chi #{expense_id}")
            except ValueError:
                await update.message.reply_text("❌ ID không hợp lệ!")
        
        # XÓA THU NHẬP
        elif text.startswith('xoa thu '):
            parts = text.split()
            if len(parts) < 3:
                await update.message.reply_text("❌ Cần có ID! VD: `xoa thu 3`")
                return
            
            try:
                income_id = int(parts[2])
                if delete_income(income_id, user_id):
                    await update.message.reply_text(f"✅ Đã xóa khoản thu #{income_id}\n\n🕐 {format_vn_time_short()}")
                else:
                    await update.message.reply_text(f"❌ Không tìm thấy khoản thu #{income_id}")
            except ValueError:
                await update.message.reply_text("❌ ID không hợp lệ!")

    # ==================== CALLBACK HANDLER ====================
    # (Giữ nguyên code callback handler từ code gốc)
    async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        try:
            # MENU CHÍNH
            if data == "back_to_main":
                await query.edit_message_text(
                    f"💰 *MENU CHÍNH*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=None
                )
                await query.message.reply_text("👇 Chọn chức năng:", reply_markup=get_main_keyboard())
            
            # ĐẦU TƯ
            elif data == "back_to_invest":
                await query.edit_message_text(
                    f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_invest_menu_keyboard()
                )
            
            elif data == "refresh_usdt":
                rate_data = get_usdt_vnd_rate()
                text = (
                    "💱 *TỶ GIÁ USDT/VND*\n━━━━━━━━━━━━━━━━\n\n"
                    f"🇺🇸 *1 USDT* = `{fmt_vnd(rate_data['vnd'])}`\n"
                    f"🇻🇳 *1,000,000 VND* = `{1000000/rate_data['vnd']:.4f} USDT`\n\n"
                    f"⏱ *Cập nhật:* `{rate_data['update_time']}`\n"
                    f"📊 *Nguồn:* `{rate_data['source']}`\n\n"
                    f"🕐 {format_vn_time()}"
                )
                keyboard = [[InlineKeyboardButton("🔄 Làm mới", callback_data="refresh_usdt")],
                            [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            
            # ... (các callback khác giữ nguyên)
            # Vì quá dài, tôi sẽ không copy toàn bộ, bạn hãy giữ nguyên phần callback từ code cũ.
            
            else:
                await query.edit_message_text("❌ Không hiểu lệnh!")
        
        except Exception as e:
            logger.error(f"Lỗi callback: {e}")
            await query.edit_message_text("❌ Có lỗi xảy ra!")

    # ==================== PORTFOLIO STATS HELPER ====================
    def get_portfolio_stats(user_id):
        try:
            portfolio_data = get_portfolio(user_id)
            if not portfolio_data:
                return None
            
            total_invest = 0
            total_value = 0
            coins = {}
            
            for row in portfolio_data:
                symbol, amount, price, date, cost = row[0], row[1], row[2], row[3], row[4]
                
                if symbol not in coins:
                    coins[symbol] = {'amount': 0, 'cost': 0}
                coins[symbol]['amount'] += amount
                coins[symbol]['cost'] += cost
                total_invest += cost
                
                price_data = get_price(symbol)
                current_price = price_data['p'] if price_data else price
                total_value += amount * current_price
            
            total_profit = total_value - total_invest
            total_profit_percent = (total_profit / total_invest * 100) if total_invest > 0 else 0
            
            coin_profits = []
            for symbol, data in coins.items():
                price_data = get_price(symbol)
                current_price = price_data['p'] if price_data else 0
                current_value = data['amount'] * current_price
                profit = current_value - data['cost']
                profit_pct = (profit / data['cost'] * 100) if data['cost'] > 0 else 0
                coin_profits.append((symbol, profit, profit_pct, current_value, data['cost']))
            
            coin_profits.sort(key=lambda x: x[1], reverse=True)
            
            return {
                'total_invest': total_invest,
                'total_value': total_value,
                'total_profit': total_profit,
                'total_profit_percent': total_profit_percent,
                'coins': coins,
                'coin_profits': coin_profits
            }
        except Exception as e:
            logger.error(f"❌ Lỗi get_portfolio_stats: {e}")
            return None

    # ==================== MAIN ====================
    if __name__ == '__main__':
        try:
            logger.info("🚀 KHỞI ĐỘNG BOT...")
            logger.info(f"🕐 Thời gian: {format_vn_time()}")
            
            if not init_database():
                logger.error("❌ KHÔNG THỂ KHỞI TẠO DATABASE")
                time.sleep(5)
            
            try:
                migrate_database()
            except Exception as e:
                logger.error(f"❌ Lỗi migrate: {e}")
            
            try:
                app = Application.builder().token(TELEGRAM_TOKEN).build()
                logger.info("✅ Đã tạo Telegram Application")
            except Exception as e:
                logger.error(f"❌ Lỗi tạo Application: {e}")
                raise
            
            # Đăng ký handlers
            app.add_handler(CommandHandler("start", start))
            app.add_handler(CommandHandler("menu", menu_command))  # THÊM LỆNH /MENU
            app.add_handler(CommandHandler("help", help_command))
            app.add_handler(CommandHandler("usdt", usdt_command))
            app.add_handler(CommandHandler("s", s_command))
            app.add_handler(CommandHandler("buy", buy_command))
            app.add_handler(CommandHandler("sell", sell_command))
            app.add_handler(CommandHandler("edit", edit_command))
            app.add_handler(CommandHandler("del", delete_tx_command))
            app.add_handler(CommandHandler("delete", delete_tx_command))
            app.add_handler(CommandHandler("xoa", delete_tx_command))
            app.add_handler(CommandHandler("alert", alert_command))
            app.add_handler(CommandHandler("alerts", alerts_command))
            app.add_handler(CommandHandler("stats", stats_command))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_handler(CallbackQueryHandler(handle_callback))
            
            # Tuỳ chọn: Tự động gửi keyboard khi bot được thêm vào nhóm
            async def welcome_new_chat_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
                for member in update.message.new_chat_members:
                    if member.id == ctx.bot.id:
                        await update.message.reply_text(
                            "🚀 *Cảm ơn đã thêm tôi vào nhóm!*\n\n"
                            "Gõ /menu để hiển thị bàn phím tiện ích.",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=get_main_keyboard()
                        )
                        break
            app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_chat_members))
            
            logger.info("✅ Đã đăng ký handlers")
            
            # Threads
            threading.Thread(target=schedule_backup, daemon=True).start()
            threading.Thread(target=schedule_cleanup, daemon=True).start()
            threading.Thread(target=check_alerts, daemon=True).start()
            threading.Thread(target=run_health_server, daemon=True).start()
            
            logger.info(f"🎉 BOT ĐÃ SẴN SÀNG! {format_vn_time()}")
            
            app.run_polling(timeout=30, drop_pending_updates=True)
            
        except Exception as e:
            logger.error(f"❌ LỖI: {e}", exc_info=True)
            time.sleep(5)
            os.execv(sys.executable, ['python'] + sys.argv)

except Exception as e:
    logger.critical(f"💥 LỖI NGHIÊM TRỌNG: {e}", exc_info=True)
    time.sleep(10)
    os.execv(sys.executable, ['python'] + sys.argv)
