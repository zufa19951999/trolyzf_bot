"""
Crypto & Expense Manager Bot - Optimized for Render
Author: Assistant
Version: 2.0 - Render Optimized
"""
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
import gc
import psutil
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.error import TelegramError
from functools import wraps
from flask import Flask, request
import asyncio

# ==================== HÀM ESCAPE MARKDOWN ====================
def escape_markdown(text):
    """Escape các ký tự đặc biệt trong markdown"""
    if not text:
        return ""
    # Danh sách ký tự cần escape trong markdown
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

# ==================== OWNER CONFIGURATION ====================
OWNER_ID = 1164334777  # Thay bằng ID của ADM
OWNER_USERNAME = "adm"  # Username của ADM

def is_owner(user_id):
    """Kiểm tra có phải là chủ sở hữu không"""
    return user_id == OWNER_ID

def get_effective_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xác định user_id thực tế:
    - Private chat: user tự quản lý data riêng
    - Group chat: TẤT CẢ data đều thuộc về OWNER của group
    """
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id
    
    if chat_type in ['group', 'supergroup']:
        group_id = update.effective_chat.id
        owner_id = get_group_owner(group_id)
        
        if not owner_id:
            # Group chưa có owner - thông báo lỗi
            logger.warning(f"⚠️ Group {group_id} chưa có owner")
            return None, user_id
        
        # TẤT CẢ data đều thuộc về OWNER
        logger.info(f"🏢 Group {group_id}: user {user_id} đang thao tác trên data của owner {owner_id}")
        return owner_id, user_id
    
    # Private chat: mỗi người data riêng
    logger.info(f"💬 Private: user {user_id} tự quản lý data riêng")
    return user_id, user_id

# ==================== USERNAME CACHE ====================
class UsernameCache:
    def __init__(self):
        self.cache = {}
        self.last_update = {}
        self.ttl = 3600  # 1 giờ
    
    def get(self, username):
        clean = username.lower().replace('@', '')
        if clean in self.cache:
            if time.time() - self.last_update.get(clean, 0) < self.ttl:
                return self.cache[clean]
        return None
    
    def set(self, username, user_id):
        if username:
            clean = username.lower().replace('@', '')
            self.cache[clean] = user_id
            self.last_update[clean] = time.time()
    
    def clear(self):
        self.cache.clear()
        self.last_update.clear()

username_cache = UsernameCache()

# ==================== RENDER CONFIGURATION ====================
class RenderConfig:
    def __init__(self):
        self.is_render = os.environ.get('RENDER', False)
        self.memory_limit = int(os.environ.get('MEMORY_LIMIT', 512))  # MB
        self.cpu_limit = float(os.environ.get('CPU_LIMIT', 1))
        self.render_url = os.environ.get('RENDER_EXTERNAL_URL')
        self.start_time = time.time()
        
    def get_worker_count(self):
        """Auto-adjust workers based on CPU"""
        if self.is_render:
            return max(1, int(self.cpu_limit) * 2)
        return 4
    
    def should_cleanup(self):
        """Check if memory cleanup needed"""
        try:
            memory_percent = psutil.virtual_memory().percent
            return memory_percent > 80
        except:
            return False

render_config = RenderConfig()

# ==================== THIẾT LẬP LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==================== THỜI GIAN VIỆT NAM ====================
def get_vn_time():
    return datetime.utcnow() + timedelta(hours=7)

def format_vn_time():
    return get_vn_time().strftime("%H:%M:%S %d/%m/%Y")

def format_vn_time_short():
    return get_vn_time().strftime("%H:%M %d/%m")

# ==================== ADVANCED CACHE SYSTEM ====================
class AdvancedCache:
    def __init__(self, name, max_size=100, ttl=300):
        self.name = name
        self.cache = {}
        self.max_size = max_size
        self.ttl = ttl
        self.hits = 0
        self.misses = 0
        
    def get(self, key):
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                self.hits += 1
                return data
            else:
                del self.cache[key]
        self.misses += 1
        return None
    
    def set(self, key, value):
        # Remove oldest if full
        if len(self.cache) >= self.max_size:
            oldest = min(self.cache.keys(), 
                        key=lambda k: self.cache[k][1])
            del self.cache[oldest]
        
        self.cache[key] = (value, time.time())
    
    def clear(self):
        self.cache.clear()
        self.hits = 0
        self.misses = 0
        logger.info(f"🧹 Cache {self.name} cleared")
    
    def get_stats(self):
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            'size': len(self.cache),
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': round(hit_rate, 2)
        }

# Initialize caches
price_cache = AdvancedCache('price', max_size=50, ttl=60)  # 1 phút
usdt_cache = AdvancedCache('usdt', max_size=1, ttl=180)    # 3 phút

# ==================== RATE LIMITING ====================
class SecurityManager:
    def __init__(self):
        self.rate_limits = {}
        self.max_requests_per_minute = 30

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
                        await update.message.reply_text(f"⚠️ Quá nhiều request. Thử lại sau 1 phút.\n\n🕐 {format_vn_time()}")
                        return
                    security.rate_limits[user_id] = (calls + 1, first_call)
                else:
                    security.rate_limits[user_id] = (1, current_time)
            else:
                security.rate_limits[user_id] = (1, current_time)
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# ==================== KHỞI TẠO ====================
try:
    load_dotenv()

    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    CMC_API_KEY = os.getenv('CMC_API_KEY')
    CMC_API_URL = "https://pro-api.coinmarketcap.com/v1"

    if not TELEGRAM_TOKEN:
        logger.error("❌ THIẾU TELEGRAM_TOKEN")
        raise ValueError("TELEGRAM_TOKEN không được để trống")
    
    if not CMC_API_KEY:
        logger.warning("⚠️ THIẾU CMC_API_KEY")

    # ==================== CẤU HÌNH DATABASE ====================
    DATA_DIR = '/data' if os.path.exists('/data') else os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(DATA_DIR, 'crypto_bot.db')
    BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
    EXPORT_DIR = os.path.join(DATA_DIR, 'exports')

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    logger.info(f"📁 Database: {DB_PATH}")
    logger.info(f"🚀 Render mode: {render_config.is_render}")

    app = None
    webhook_app = Flask(__name__)

    # ==================== DATABASE OPTIMIZATION ====================
    def optimize_database():
        """Nén database và xóa dữ liệu cũ"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # VACUUM để nén database
            c.execute("VACUUM")
            
            # Xóa alerts cũ (hơn 30 ngày)
            c.execute('''DELETE FROM alerts 
                         WHERE triggered_at IS NOT NULL 
                         AND date(triggered_at) < date('now', '-30 days')''')
            
            conn.commit()
            conn.close()
            
            # Clean log file
            if os.path.exists('bot.log'):
                with open('bot.log', 'r') as f:
                    lines = f.readlines()
                if len(lines) > 1000:
                    with open('bot.log', 'w') as f:
                        f.writelines(lines[-1000:])
            
            # Tính dung lượng
            size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
            logger.info(f"✅ Database optimized: {size_mb:.2f}MB")
            
        except Exception as e:
            logger.error(f"❌ Lỗi optimize DB: {e}")

    # ==================== MEMORY MONITOR ====================
    def check_memory_usage():
        """Kiểm tra memory và cleanup nếu cần"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024
            cpu_percent = process.cpu_percent()
            
            logger.info(f"📊 Memory: {memory_mb:.2f}MB | CPU: {cpu_percent:.1f}% | "
                       f"Cache: P{price_cache.get_stats()['size']}/U{usdt_cache.get_stats()['size']}")
            
            # Nếu dùng quá 70% memory limit
            if memory_mb > render_config.memory_limit * 0.7:
                logger.warning("⚠️ Memory high, cleaning caches...")
                price_cache.clear()
                usdt_cache.clear()
                gc.collect()
                
            # Nếu vẫn cao sau cleanup
            if memory_mb > render_config.memory_limit * 0.9:
                logger.critical("💥 Memory critical, restarting...")
                sys.exit(1)  # Render sẽ tự restart
                
        except Exception as e:
            logger.error(f"❌ Memory check error: {e}")

    def memory_monitor():
        while True:
            check_memory_usage()
            time.sleep(300)  # Check mỗi 5 phút

    # ==================== DATABASE SETUP ====================
    def init_database():
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS portfolio
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER, symbol TEXT, amount REAL,
                          buy_price REAL, buy_date TEXT, total_cost REAL)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS alerts
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER, symbol TEXT, target_price REAL,
                          condition TEXT, is_active INTEGER DEFAULT 1,
                          created_at TEXT, triggered_at TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS expense_categories
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER, name TEXT, budget REAL, created_at TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS expenses
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER, category_id INTEGER, amount REAL,
                          currency TEXT DEFAULT 'VND', note TEXT,
                          expense_date TEXT, created_at TEXT,
                          FOREIGN KEY (category_id) REFERENCES expense_categories(id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS incomes
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER, amount REAL, currency TEXT DEFAULT 'VND',
                          source TEXT, income_date TEXT, note TEXT, created_at TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS users
                         (user_id INTEGER PRIMARY KEY,
                          username TEXT, first_name TEXT, last_name TEXT, last_seen TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS group_admins
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      group_id INTEGER,
                      admin_id INTEGER,
                      granted_by INTEGER,
                      can_view INTEGER DEFAULT 0,
                      can_edit INTEGER DEFAULT 0,
                      can_delete INTEGER DEFAULT 0,
                      can_manage INTEGER DEFAULT 0,
                      created_at TEXT,
                      UNIQUE(group_id, admin_id))''')

            c.execute('''CREATE TABLE IF NOT EXISTS permission_logs
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          group_id INTEGER,
                          action_by INTEGER,
                          target_user INTEGER,
                          action TEXT,
                          old_role TEXT,
                          new_role TEXT,
                          created_at TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS group_owners
                         (group_id INTEGER PRIMARY KEY,
                          owner_id INTEGER,
                          created_at TEXT)''')
            
            conn.commit()
            logger.info(f"✅ Database initialized with enhanced permissions")
            return True
        
        except Exception as e:
            logger.error(f"❌ Lỗi database: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def auto_migrate_permissions():
        """Tự động migrate permissions table nếu cần"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Kiểm tra bảng permissions có tồn tại không
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='permissions'")
            if not c.fetchone():
                conn.close()
                return
            
            # Kiểm tra cấu trúc hiện tại
            c.execute("PRAGMA table_info(permissions)")
            columns = [col[1] for col in c.fetchall()]
            
            # Nếu là cấu trúc cũ, thêm các cột mới nếu chưa có
            if 'admin_id' in columns and 'user_id' not in columns:
                logger.info("🔄 Đang migrate permissions table...")
                
                # Thêm cột user_id
                try:
                    c.execute("ALTER TABLE permissions ADD COLUMN user_id INTEGER")
                except:
                    pass
                
                # Copy dữ liệu từ admin_id sang user_id
                c.execute("UPDATE permissions SET user_id = admin_id WHERE user_id IS NULL")
                
                # Thêm các cột mới
                try:
                    c.execute("ALTER TABLE permissions ADD COLUMN is_approved INTEGER DEFAULT 1")
                except:
                    pass
                
                try:
                    c.execute("ALTER TABLE permissions ADD COLUMN role TEXT DEFAULT 'staff'")
                except:
                    pass
                
                try:
                    c.execute("ALTER TABLE permissions ADD COLUMN approved_at TEXT")
                except:
                    pass
                
                conn.commit()
                logger.info("✅ Migrate permissions thành công!")
            
            conn.close()
        except Exception as e:
            logger.error(f"❌ Lỗi migrate: {e}")

    def migrate_permissions_table():
        """Migrate bảng permissions từ cấu trúc cũ sang mới"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Kiểm tra xem bảng permissions có tồn tại không
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='permissions'")
            if not c.fetchone():
                conn.close()
                return
            
            # Kiểm tra cấu trúc hiện tại
            c.execute("PRAGMA table_info(permissions)")
            columns = [col[1] for col in c.fetchall()]
            
            # Nếu là cấu trúc cũ (có cột admin_id)
            if 'admin_id' in columns and 'user_id' not in columns:
                logger.info("🔄 Migrating old permissions table...")
                
                # Tạo bảng tạm
                c.execute('''CREATE TABLE permissions_new
                             (id INTEGER PRIMARY KEY AUTOINCREMENT,
                              group_id INTEGER, 
                              user_id INTEGER, 
                              granted_by INTEGER,
                              is_approved INTEGER DEFAULT 1,
                              role TEXT DEFAULT 'staff',
                              can_view_all INTEGER DEFAULT 0,
                              can_edit_all INTEGER DEFAULT 0,
                              can_delete_all INTEGER DEFAULT 0,
                              can_manage_perms INTEGER DEFAULT 0,
                              created_at TEXT,
                              approved_at TEXT,
                              UNIQUE(group_id, user_id))''')
                
                # Copy dữ liệu từ bảng cũ
                c.execute('''INSERT INTO permissions_new 
                             (group_id, user_id, granted_by, can_view_all, can_edit_all, 
                              can_delete_all, can_manage_perms, created_at, is_approved, role)
                             SELECT group_id, admin_id, granted_by, can_view_all, can_edit_all,
                                    can_delete_all, can_manage_perms, created_at, 1, 'staff'
                             FROM permissions''')
                
                # Xóa bảng cũ
                c.execute("DROP TABLE permissions")
                
                # Đổi tên bảng mới
                c.execute("ALTER TABLE permissions_new RENAME TO permissions")
                
                conn.commit()
                logger.info("✅ Permissions table migrated successfully")
            
            conn.close()
        except Exception as e:
            logger.error(f"❌ Lỗi migrate permissions: {e}")

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
            logger.error(f"❌ Lỗi migrate: {e}")
        finally:
            if conn:
                conn.close()
                
    def backup_database():
        try:
            if os.path.exists(DB_PATH):
                # Chỉ backup nếu database > 1MB
                if os.path.getsize(DB_PATH) > 1024 * 1024:
                    timestamp = get_vn_time().strftime('%Y%m%d_%H%M%S')
                    backup_path = os.path.join(BACKUP_DIR, f'backup_{timestamp}.db')
                    shutil.copy2(DB_PATH, backup_path)
                    
                    # Xóa backup cũ hơn 7 ngày
                    for f in os.listdir(BACKUP_DIR):
                        f_path = os.path.join(BACKUP_DIR, f)
                        if os.path.getctime(f_path) < time.time() - 7 * 86400:
                            os.remove(f_path)
        except Exception as e:
            logger.error(f"❌ Lỗi backup: {e}")

    def schedule_backup():
        while True:
            try:
                backup_database()
                time.sleep(86400)  # 24h
            except:
                time.sleep(3600)

    # ==================== BATCH PRICE FETCHING ====================
    def get_prices_batch(symbols):
        """Lấy giá nhiều coin cùng lúc"""
        try:
            if not CMC_API_KEY or not symbols:
                return {}
            
            # Check cache trước
            results = {}
            uncached = []
            
            for symbol in symbols:
                cached = price_cache.get(symbol)
                if cached:
                    results[symbol] = cached
                else:
                    uncached.append(symbol)
            
            if uncached:
                # Gom nhóm theo từng 10 coin
                for i in range(0, len(uncached), 10):
                    batch = uncached[i:i+10]
                    symbols_str = ','.join(batch)
                    
                    headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY}
                    params = {'symbol': symbols_str, 'convert': 'USD'}
                    
                    res = requests.get(
                        f"{CMC_API_URL}/cryptocurrency/quotes/latest",
                        headers=headers,
                        params=params,
                        timeout=10
                    )
                    
                    if res.status_code == 200:
                        data = res.json()
                        for symbol in batch:
                            if symbol in data['data']:
                                coin_data = data['data'][symbol]
                                quote = coin_data['quote']['USD']
                                result = {
                                    'p': quote['price'],
                                    'v': quote['volume_24h'],
                                    'c': quote['percent_change_24h'],
                                    'm': quote['market_cap'],
                                    'n': coin_data['name'],
                                    'r': coin_data.get('cmc_rank', 'N/A')
                                }
                                results[symbol] = result
                                price_cache.set(symbol, result)
                    
                    time.sleep(0.5)  # Tránh rate limit
            
            return results
        except Exception as e:
            logger.error(f"❌ Batch price error: {e}")
            return {}

    def get_price(symbol):
        """Lấy giá 1 coin (có cache)"""
        cached = price_cache.get(symbol)
        if cached:
            return cached
            
        try:
            if not CMC_API_KEY:
                return None
                
            clean_symbol = symbol.upper()
            if clean_symbol == 'USDT':
                clean = 'USDT'
            else:
                clean = clean_symbol.replace('USDT', '').replace('USD', '')
            
            headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY}
            params = {'symbol': clean, 'convert': 'USD'}
            
            res = requests.get(f"{CMC_API_URL}/cryptocurrency/quotes/latest", 
                              headers=headers, params=params, timeout=10)
            
            if res.status_code == 200:
                data = res.json()
                if 'data' not in data or clean not in data['data']:
                    return None
                    
                coin_data = data['data'][clean]
                quote_data = coin_data['quote']['USD']
                
                result = {
                    'p': quote_data['price'],
                    'v': quote_data['volume_24h'],
                    'c': quote_data['percent_change_24h'],
                    'm': quote_data['market_cap'],
                    'n': coin_data['name'],
                    'r': coin_data.get('cmc_rank', 'N/A')
                }
                price_cache.set(symbol, result)
                return result
            else:
                return None
        except Exception as e:
            logger.error(f"❌ Lỗi get_price {symbol}: {e}")
            return None

    def get_usdt_vnd_rate():
        """Lấy tỷ giá USDT/VND (có cache)"""
        cached = usdt_cache.get('rate')
        if cached:
            return cached
        
        try:
            # Thử CoinGecko trước
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
                        usdt_cache.set('rate', result)
                        return result
            except:
                pass
            
            # Fallback
            result = {
                'source': 'Fallback (25000)',
                'vnd': 25000,
                'update_time': format_vn_time()
            }
            usdt_cache.set('rate', result)
            return result
        except Exception as e:
            logger.error(f"❌ Lỗi get_usdt_vnd_rate: {e}")
            return {'source': 'Error', 'vnd': 25000, 'update_time': format_vn_time()}

    # ==================== PORTFOLIO FUNCTIONS ====================
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
            logger.error(f"❌ Lỗi thêm transaction: {e}")
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
            logger.error(f"❌ Lỗi lấy portfolio: {e}")
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
            logger.error(f"❌ Lỗi lấy transaction: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def delete_transaction(transaction_id, user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''DELETE FROM portfolio WHERE id = ? AND user_id = ?''',
                      (transaction_id, user_id))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Lỗi xóa transaction: {e}")
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
                         FROM alerts WHERE user_id = ? AND is_active = 1 
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
                        msg = (f"🔔 *CẢNH BÁO GIÁ*\n━━━━━━━━━━━━━━━━\n\n"
                               f"• Coin: *{symbol}*\n"
                               f"• Giá hiện: `{fmt_price(current_price)}`\n"
                               f"• Mốc: `{fmt_price(target_price)}`\n"
                               f"• Điều kiện: {'📈 Lên trên' if condition == 'above' else '📉 Xuống dưới'}\n\n"
                               f"🕐 {format_vn_time()}")
                        
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

    # ==================== PERMISSIONS FUNCTIONS ====================
    def grant_permission(group_id, user_id, granted_by, permissions):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            # Xóa quyền cũ
            c.execute("DELETE FROM permissions WHERE group_id = ? AND user_id = ?", 
                      (group_id, user_id))
            
            # Thêm quyền mới với role là 'staff' cho admin
            c.execute('''INSERT INTO permissions 
                         (group_id, user_id, granted_by, is_approved, role,
                          can_view_all, can_edit_all, can_delete_all, can_manage_perms,
                          created_at, approved_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (group_id, user_id, granted_by,
                       1, 'staff',
                       permissions.get('view', 0),
                       permissions.get('edit', 0),
                       permissions.get('delete', 0),
                       permissions.get('manage', 0),
                       created_at, created_at))
            
            conn.commit()
            logger.info(f"✅ Granted permissions to user {user_id} in group {group_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi cấp quyền: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def revoke_permission(group_id, user_id):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            # ĐÃ SỬA: dùng user_id thay vì admin_id
            c.execute("DELETE FROM permissions WHERE group_id = ? AND user_id = ?", 
                      (group_id, user_id))
            conn.commit()
            affected = c.rowcount
            conn.close()
            
            if affected > 0:
                logger.info(f"✅ Đã thu hồi quyền của user {user_id} trong group {group_id}")
                return True
            else:
                logger.info(f"ℹ️ Không tìm thấy quyền của user {user_id} trong group {group_id}")
                return False
        except Exception as e:
            logger.error(f"❌ Lỗi thu hồi quyền: {e}")
            return False

    def check_user_access(group_id, user_id, required_role='user'):
        try:
            if is_owner(user_id):
                return True
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            c.execute('''SELECT role, is_approved, can_view_all, can_edit_all, can_delete_all, can_manage_perms 
                         FROM permissions 
                         WHERE group_id = ? AND user_id = ?''',
                      (group_id, user_id))
            result = c.fetchone()
            conn.close()
            
            if not result:
                return False
            
            role, is_approved, can_view, can_edit, can_delete, can_manage = result
            
            if is_approved == 0:
                return False
            
            if required_role == 'owner':
                return role == 'owner'
            elif required_role == 'staff':
                return role in ['owner', 'staff']
            elif required_role == 'user':
                return role in ['owner', 'staff', 'user']
            
            return False
        except Exception as e:
            logger.error(f"❌ Lỗi check_user_access: {e}")
            return False
    
    def get_user_permissions(group_id, user_id):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT role, is_approved, can_view_all, can_edit_all, 
                                can_delete_all, can_manage_perms, created_at, approved_at
                         FROM permissions 
                         WHERE group_id = ? AND user_id = ?''',
                      (group_id, user_id))
            result = c.fetchone()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"❌ Lỗi get_user_permissions: {e}")
            return None
    
    def grant_user_access(group_id, target_user_id, granted_by, role='user'):
        """
        Cấp quyền cho user
        role: 'staff' (nhân viên), 'user' (người dùng thường)
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            # Xóa quyền cũ nếu có
            c.execute("DELETE FROM permissions WHERE group_id = ? AND user_id = ?", 
                      (group_id, target_user_id))
            
            # Set quyền dựa vào role
            if role == 'staff':
                # Staff có quyền quản lý dữ liệu
                permissions = {
                    'is_approved': 1,
                    'role': 'staff',
                    'view': 1,
                    'edit': 1,
                    'delete': 1,
                    'manage': 0  # Staff không được quản lý phân quyền
                }
            else:  # user
                # User chỉ được xem
                permissions = {
                    'is_approved': 1,
                    'role': 'user',
                    'view': 1,
                    'edit': 0,
                    'delete': 0,
                    'manage': 0
                }
            
            c.execute('''INSERT INTO permissions 
                         (group_id, user_id, granted_by, is_approved, role,
                          can_view_all, can_edit_all, can_delete_all, can_manage_perms,
                          created_at, approved_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (group_id, target_user_id, granted_by,
                       permissions['is_approved'], permissions['role'],
                       permissions['view'], permissions['edit'],
                       permissions['delete'], permissions['manage'],
                       created_at, created_at))
            
            # Ghi log
            c.execute('''INSERT INTO permission_logs
                         (group_id, action_by, target_user, action, new_role, created_at)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (group_id, granted_by, target_user_id, 'GRANT', role, created_at))
            
            conn.commit()
            conn.close()
            
            logger.info(f"✅ Granted {role} access to user {target_user_id} in group {group_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi grant_user_access: {e}")
            return False
    
    def approve_user(group_id, target_user_id, approved_by):
        """Duyệt user (chờ cấp quyền)"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            approved_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute('''UPDATE permissions 
                         SET is_approved = 1, approved_at = ?
                         WHERE group_id = ? AND user_id = ?''',
                      (approved_at, group_id, target_user_id))
            
            affected = c.rowcount
            conn.commit()
            conn.close()
            
            if affected > 0:
                logger.info(f"✅ Approved user {target_user_id} in group {group_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Lỗi approve_user: {e}")
            return False

    def require_permission(required_role='user'):
        """Decorator yêu cầu quyền truy cập"""
        def decorator(func):
            @wraps(func)
            async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
                user_id = update.effective_user.id
                chat_id = update.effective_chat.id
                chat_type = update.effective_chat.type
                
                # Owner luôn được phép
                if is_owner(user_id):
                    return await func(update, context, *args, **kwargs)
                
                # Trong private chat, tự động cho phép (sẽ check sau)
                if chat_type == 'private':
                    return await func(update, context, *args, **kwargs)
                
                # Trong group, kiểm tra quyền
                if chat_type in ['group', 'supergroup']:
                    if not check_user_access(chat_id, user_id, required_role):
                        await update.message.reply_text(
                            f"❌ Bạn chưa được cấp quyền sử dụng bot trong group này!\n\n"
                            f"Vui lòng liên hệ @{OWNER_USERNAME} để được cấp quyền.",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return
                
                return await func(update, context, *args, **kwargs)
            return wrapper
        return decorator

    def require_group_permission(required_permission='view'):
        """Decorator kiểm tra quyền của user trong group"""
        def decorator(func):
            @wraps(func)
            async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
                chat_type = update.effective_chat.type
                current_user = update.effective_user.id
                effective_user = context.bot_data.get('effective_user_id')
                
                # Private chat - ai cũng dùng được
                if chat_type == 'private':
                    return await func(update, context, *args, **kwargs)
                
                # Group chat - kiểm tra quyền
                if chat_type in ['group', 'supergroup']:
                    group_id = update.effective_chat.id
                    
                    # OWNER có toàn quyền
                    if current_user == effective_user:
                        return await func(update, context, *args, **kwargs)
                    
                    # Kiểm tra quyền admin
                    if not check_admin_permission(group_id, current_user, required_permission):
                        await update.message.reply_text(
                            f"❌ Bạn không có quyền {required_permission} trong group này!\n"
                            f"Vui lòng liên hệ chủ sở hữu group.",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return
                    
                    # QUAN TRỌNG: Đảm bảo effective_user_id vẫn là owner
                    context.bot_data['effective_user_id'] = effective_user
                    return await func(update, context, *args, **kwargs)
                
                return await func(update, context, *args, **kwargs)
            return wrapper
        return decorator

    def check_permission(group_id, user_id, permission_type='view'):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Dùng đúng tên cột: can_view_all, can_edit_all, can_delete_all, can_manage_perms
            c.execute('''SELECT can_view_all, can_edit_all, can_delete_all, can_manage_perms 
                         FROM permissions 
                         WHERE group_id = ? AND user_id = ?''',
                      (group_id, user_id))
            result = c.fetchone()
            
            if not result:
                logger.info(f"🔍 User {user_id} không có quyền trong group {group_id}")
                return False
            
            can_view, can_edit, can_delete, can_manage = result
            
            if permission_type == 'view':
                return can_view == 1
            elif permission_type == 'edit':
                return can_edit == 1
            elif permission_type == 'delete':
                return can_delete == 1
            elif permission_type == 'manage':
                return can_manage == 1
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Lỗi check_permission: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_all_admins(group_id):
        """Lấy danh sách tất cả admin trong group"""
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # SỬA: Dùng bảng group_admins thay vì permissions
            c.execute('''SELECT ga.admin_id, ga.can_view, ga.can_edit, ga.can_delete, ga.can_manage,
                                u.username, u.first_name, ga.created_at
                         FROM group_admins ga
                         LEFT JOIN users u ON ga.admin_id = u.user_id
                         WHERE ga.group_id = ?
                         ORDER BY ga.created_at''', (group_id,))
            admins = c.fetchall()
            conn.close()
            return admins
        except Exception as e:
            logger.error(f"❌ Lỗi get_all_admins: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def grant_admin_permission(group_id, admin_id, granted_by, permissions):
        """Cấp quyền admin (chỉ owner mới được dùng)"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            # Xóa quyền cũ nếu có
            c.execute("DELETE FROM group_admins WHERE group_id = ? AND admin_id = ?", 
                      (group_id, admin_id))
            
            # Thêm quyền mới
            c.execute('''INSERT INTO group_admins 
                         (group_id, admin_id, granted_by, can_view, can_edit, can_delete, can_manage, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                      (group_id, admin_id, granted_by,
                       permissions.get('view', 0),
                       permissions.get('edit', 0),
                       permissions.get('delete', 0),
                       permissions.get('manage', 0),
                       created_at))
            
            conn.commit()
            conn.close()
            logger.info(f"✅ Granted admin permissions to {admin_id} in group {group_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi grant admin: {e}")
            return False

    def revoke_admin_permission(group_id, admin_id):
        """Thu hồi quyền admin"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM group_admins WHERE group_id = ? AND admin_id = ?", 
                      (group_id, admin_id))
            conn.commit()
            affected = c.rowcount
            conn.close()
            
            if affected > 0:
                logger.info(f"✅ Revoked admin permissions from {admin_id} in group {group_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Lỗi revoke admin: {e}")
            return False

    def check_admin_permission(group_id, admin_id, permission='view'):
        """Kiểm tra quyền của admin"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT can_view, can_edit, can_delete, can_manage 
                         FROM group_admins 
                         WHERE group_id = ? AND admin_id = ?''',
                      (group_id, admin_id))
            result = c.fetchone()
            conn.close()
            
            if not result:
                return False
            
            can_view, can_edit, can_delete, can_manage = result
            
            if permission == 'view':
                return can_view == 1
            elif permission == 'edit':
                return can_edit == 1
            elif permission == 'delete':
                return can_delete == 1
            elif permission == 'manage':
                return can_manage == 1
            
            return False
        except Exception as e:
            logger.error(f"❌ Lỗi check_admin: {e}")
            return False

    # ==================== GROUP OWNER MANAGEMENT ====================
    GROUP_OWNERS = {}
    
    def load_group_owners():
        """Load group owners từ database"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT group_id, owner_id FROM group_owners")
            rows = c.fetchall()
            for group_id, owner_id in rows:
                GROUP_OWNERS[group_id] = owner_id
            conn.close()
            logger.info(f"✅ Loaded {len(GROUP_OWNERS)} group owners")
        except Exception as e:
            logger.error(f"❌ Lỗi load group owners: {e}")
    
    def set_group_owner(group_id, owner_id):
        """Thiết lập chủ sở hữu cho group"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            c.execute('''INSERT OR REPLACE INTO group_owners (group_id, owner_id, created_at)
                         VALUES (?, ?, ?)''', (group_id, owner_id, created_at))
            conn.commit()
            conn.close()
            GROUP_OWNERS[group_id] = owner_id
            logger.info(f"✅ Set owner {owner_id} for group {group_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi set group owner: {e}")
            return False
    
    def get_group_owner(group_id):
        """Lấy chủ sở hữu của group"""
        return GROUP_OWNERS.get(group_id, OWNER_ID)
    
    def is_group_owner(group_id, user_id):
        """Kiểm tra có phải chủ sở hữu của group không"""
        return user_id == get_group_owner(group_id)
    
    # ==================== USER FUNCTIONS WITH AUTO-UPDATE ====================
    async def update_user_info_async(user):
        """Cập nhật thông tin user bất đồng bộ - gọi mỗi khi có tương tác"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            current_time = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            # Kiểm tra user đã tồn tại chưa
            c.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
            exists = c.fetchone()
            
            if exists:
                # Cập nhật thông tin
                c.execute('''UPDATE users SET 
                             username = ?, 
                             first_name = ?, 
                             last_name = ?, 
                             last_seen = ?
                             WHERE user_id = ?''',
                          (user.username, 
                           user.first_name, 
                           user.last_name, 
                           current_time, 
                           user.id))
            else:
                # Thêm mới
                c.execute('''INSERT INTO users 
                             (user_id, username, first_name, last_name, last_seen)
                             VALUES (?, ?, ?, ?, ?)''',
                          (user.id, 
                           user.username, 
                           user.first_name, 
                           user.last_name, 
                           current_time))
            
            conn.commit()
            conn.close()
            
            # Update cache nếu có username
            if user.username:
                username_cache.set(user.username, user.id)
            
            logger.info(f"✅ Updated user {user.id} (@{user.username})")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi cập nhật user {user.id}: {e}")
            return False
    
    def update_user_info_sync(user):
        """Phiên bản đồng bộ cho các thread không async"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            current_time = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute('''INSERT OR REPLACE INTO users 
                         (user_id, username, first_name, last_name, last_seen)
                         VALUES (?, ?, ?, ?, ?)''',
                      (user.id, 
                       user.username, 
                       user.first_name, 
                       user.last_name, 
                       current_time))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi update_user_info_sync: {e}")
            return False
            
    # ==================== AUTO UPDATE USER DECORATOR ====================
    
    def auto_update_user(func):
        """Decorator tự động cập nhật user info và xác định effective user"""
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            if update.effective_user:
                await update_user_info_async(update.effective_user)
            
            # Lấy effective user_id và current user
            effective_user_id, current_user_id = get_effective_user_id(update, context)
            
            # Kiểm tra group đã có owner chưa
            if effective_user_id is None:
                chat_type = update.effective_chat.type
                if chat_type in ['group', 'supergroup']:
                    await update.message.reply_text(
                        f"❌ Group này chưa được cài đặt chủ sở hữu!\n"
                        f"Vui lòng liên hệ @{OWNER_USERNAME} để thiết lập.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
            
            context.bot_data['effective_user_id'] = effective_user_id
            context.bot_data['current_user_id'] = current_user_id
            
            logger.info(f"🆔 Current: {current_user_id}, Data Owner: {effective_user_id}")
            
            return await func(update, context, *args, **kwargs)
        return wrapper
        
    # ==================== USERNAME CACHE & LOOKUP ====================
    def get_user_id_by_username(username):
        """Tìm user ID từ username - hỗ trợ cache"""
        conn = None
        try:
            # Xử lý username
            clean_username = username.lower().replace('@', '').strip()
            
            # Kiểm tra cache trước
            cached_id = username_cache.get(clean_username)
            if cached_id:
                logger.info(f"Cache hit for @{clean_username}: {cached_id}")
                return cached_id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Tìm chính xác
            c.execute("SELECT user_id FROM users WHERE username = ?", (clean_username,))
            result = c.fetchone()
            
            if result:
                user_id = result[0]
                username_cache.set(clean_username, user_id)
                return user_id
            
            # Tìm gần đúng (nếu không tìm thấy chính xác)
            c.execute("SELECT user_id, username FROM users WHERE username LIKE ?", 
                      (f"%{clean_username}%",))
            results = c.fetchall()
            
            if results:
                # Nếu có nhiều kết quả, chọn cái đầu tiên
                user_id = results[0][0]
                username_cache.set(clean_username, user_id)
                logger.info(f"Found {len(results)} users matching '{username}', using first: {user_id}")
                return user_id
            
            return None
        except Exception as e:
            logger.error(f"❌ Lỗi tìm user {username}: {e}")
            return None
        finally:
            if conn:
                conn.close()
            
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
        'VND': '🇻🇳 Việt Nam Đồng',
        'USD': '🇺🇸 US Dollar',
        'USDT': '💵 Tether',
        'KHR': '🇰🇭 Riel Campuchia',
        'LKR': '🇱🇰 Sri Lanka Rupee'
    }

    # ==================== EXPENSE FUNCTIONS ====================
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

    def get_expense_categories(owner_id): 
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT id, name, budget, created_at 
                         FROM expense_categories WHERE user_id = ? 
                         ORDER BY name''', (owner_id,))  # Dùng owner_id
            return c.fetchall()
        except Exception as e:
            logger.error(f"❌ Lỗi lấy categories: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def add_income(owner_id, amount, source, currency='VND', note=""):
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
                      (owner_id, amount, source, income_date, note, created_at, currency))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi thêm income: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def add_expense(owner_id, category_id, amount, currency='VND', note=""):
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
                      (owner_id, category_id, amount, note, expense_date, created_at, currency))
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
                         FROM incomes WHERE user_id = ?
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
                query = '''SELECT id, amount, source, note, currency, income_date
                          FROM incomes WHERE user_id = ? AND income_date = ?
                          ORDER BY income_date DESC, created_at DESC'''
                c.execute(query, (user_id, date_filter))
            elif period == 'month':
                month_filter = now.strftime("%Y-%m")
                query = '''SELECT id, amount, source, note, currency, income_date
                          FROM incomes WHERE user_id = ? AND strftime('%Y-%m', income_date) = ?
                          ORDER BY income_date DESC, created_at DESC'''
                c.execute(query, (user_id, month_filter))
            else:
                year_filter = now.strftime("%Y")
                query = '''SELECT id, amount, source, note, currency, income_date
                          FROM incomes WHERE user_id = ? AND strftime('%Y', income_date) = ?
                          ORDER BY income_date DESC, created_at DESC'''
                c.execute(query, (user_id, year_filter))
            
            rows = c.fetchall()
            
            summary = {}
            for row in rows:
                id, amount, source, note, currency, date = row
                if currency not in summary:
                    summary[currency] = 0
                summary[currency] += amount
            
            return {
                'transactions': rows,
                'summary': summary,
                'total_count': len(rows)
            }
        except Exception as e:
            logger.error(f"❌ Lỗi income summary: {e}")
            return {'transactions': [], 'summary': {}, 'total_count': 0}
        finally:
            if conn:
                conn.close()

    def format_balance_message(balance_data, user_name=""):
        """Định dạng tin nhắn cân đối thu chi"""
        if not balance_data:
            return "❌ Không có dữ liệu để hiển thị!"
        
        msg = f"⚖️ *CÂN ĐỐI THU CHI - {balance_data['title']}*"
        if user_name:
            msg += f" - {user_name}"
        msg += "\n━━━━━━━━━━━━━━━━\n\n"
        
        # Hiển thị theo từng loại tiền
        for b in balance_data['balances']:
            currency = b['currency']
            income = b['income']
            expense = b['expense']
            balance = b['balance']
            
            if income > 0 or expense > 0:
                msg += f"*{currency}:*\n"
                if income > 0:
                    msg += f"  💰 Thu: {format_currency_simple(income, currency)}\n"
                if expense > 0:
                    msg += f"  💸 Chi: {format_currency_simple(expense, currency)}\n"
                
                # Hiển thị cân đối
                if balance > 0:
                    msg += f"  ✅ Dư: {format_currency_simple(balance, currency)}\n"
                elif balance < 0:
                    msg += f"  ❌ Thiếu: {format_currency_simple(abs(balance), currency)}\n"
                else:
                    msg += f"  ➖ Cân bằng\n"
                msg += "\n"
        
        # Tổng kết bằng VND
        if balance_data['total_income_vnd'] > 0 or balance_data['total_expense_vnd'] > 0:
            msg += "*📊 TỔNG KẾT (VND):*\n"
            msg += f"  💰 Tổng thu: {format_currency_simple(balance_data['total_income_vnd'], 'VND')}\n"
            msg += f"  💸 Tổng chi: {format_currency_simple(balance_data['total_expense_vnd'], 'VND')}\n"
            
            total_balance = balance_data['total_balance_vnd']
            if total_balance > 0:
                msg += f"  ✅ Còn lại: {format_currency_simple(total_balance, 'VND')}\n"
            elif total_balance < 0:
                msg += f"  ❌ Thiếu: {format_currency_simple(abs(total_balance), 'VND')}\n"
            else:
                msg += f"  ➖ Cân bằng\n"
        
        msg += f"\n📊 Thống kê: {balance_data['income_count']} khoản thu, {balance_data['expense_count']} khoản chi"
        msg += f"\n\n🕐 {format_vn_time()}"
        
        return msg

    def get_expenses_by_period(user_id, period='month'):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            now = get_vn_time()
            
            if period == 'day':
                date_filter = now.strftime("%Y-%m-%d")
                query = '''SELECT e.id, ec.name, e.amount, e.note, e.currency, e.expense_date, ec.budget
                          FROM expenses e
                          JOIN expense_categories ec ON e.category_id = ec.id
                          WHERE e.user_id = ? AND e.expense_date = ?
                          ORDER BY e.expense_date DESC, e.created_at DESC'''
                c.execute(query, (user_id, date_filter))
            elif period == 'month':
                month_filter = now.strftime("%Y-%m")
                query = '''SELECT e.id, ec.name, e.amount, e.note, e.currency, e.expense_date, ec.budget
                          FROM expenses e
                          JOIN expense_categories ec ON e.category_id = ec.id
                          WHERE e.user_id = ? AND strftime('%Y-%m', e.expense_date) = ?
                          ORDER BY e.expense_date DESC, e.created_at DESC'''
                c.execute(query, (user_id, month_filter))
            else:
                year_filter = now.strftime("%Y")
                query = '''SELECT e.id, ec.name, e.amount, e.note, e.currency, e.expense_date, ec.budget
                          FROM expenses e
                          JOIN expense_categories ec ON e.category_id = ec.id
                          WHERE e.user_id = ? AND strftime('%Y', e.expense_date) = ?
                          ORDER BY e.expense_date DESC, e.created_at DESC'''
                c.execute(query, (user_id, year_filter))
            
            rows = c.fetchall()
            
            summary = {}
            category_summary = {}
            
            for row in rows:
                id, cat_name, amount, note, currency, date, budget = row
                if currency not in summary:
                    summary[currency] = 0
                summary[currency] += amount
                
                key = f"{cat_name}_{currency}"
                if key not in category_summary:
                    category_summary[key] = {
                        'category': cat_name,
                        'currency': currency,
                        'total': 0,
                        'count': 0,
                        'budget': budget
                    }
                category_summary[key]['total'] += amount
                category_summary[key]['count'] += 1
            
            return {
                'transactions': rows,
                'summary': summary,
                'category_summary': category_summary,
                'total_count': len(rows)
            }
        except Exception as e:
            logger.error(f"❌ Lỗi expenses summary: {e}")
            return {'transactions': [], 'summary': {}, 'category_summary': {}, 'total_count': 0}
        finally:
            if conn:
                conn.close()

    def get_balance_summary(user_id, period='month'):
        """
        Tính cân đối thu chi theo kỳ
        period: 'day', 'month', 'year', 'all'
        """
        try:
            # Lấy dữ liệu thu chi
            if period == 'day':
                incomes = get_income_by_period(user_id, 'day')
                expenses = get_expenses_by_period(user_id, 'day')
                title = f"HÔM NAY ({get_vn_time().strftime('%d/%m/%Y')})"
            elif period == 'month':
                incomes = get_income_by_period(user_id, 'month')
                expenses = get_expenses_by_period(user_id, 'month')
                title = f"THÁNG {get_vn_time().strftime('%m/%Y')}"
            elif period == 'year':
                incomes = get_income_by_period(user_id, 'year')
                expenses = get_expenses_by_period(user_id, 'year')
                title = f"NĂM {get_vn_time().strftime('%Y')}"
            else:  # all time
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                
                # Lấy tất cả thu nhập
                c.execute('''SELECT currency, SUM(amount) FROM incomes 
                             WHERE user_id = ? GROUP BY currency''', (user_id,))
                income_rows = c.fetchall()
                
                # Lấy tất cả chi tiêu
                c.execute('''SELECT currency, SUM(amount) FROM expenses 
                             WHERE user_id = ? GROUP BY currency''', (user_id,))
                expense_rows = c.fetchall()
                
                conn.close()
                
                # Chuyển thành dict
                incomes = {'summary': {}}
                expenses = {'summary': {}}
                
                for currency, total in income_rows:
                    incomes['summary'][currency] = total
                
                for currency, total in expense_rows:
                    expenses['summary'][currency] = total
                
                title = "TỔNG KẾT TẤT CẢ"
            
            # Tính cân đối
            all_currencies = set(list(incomes['summary'].keys()) + list(expenses['summary'].keys()))
            
            balance_data = []
            total_income_vnd = 0
            total_expense_vnd = 0
            
            # Lấy tỷ giá USDT nếu cần
            usdt_rate = get_usdt_vnd_rate()['vnd'] if 'USDT' in all_currencies else None
            
            for currency in all_currencies:
                income = incomes['summary'].get(currency, 0)
                expense = expenses['summary'].get(currency, 0)
                balance = income - expense
                
                # Quy đổi ra VND để tính tổng
                if currency == 'VND':
                    total_income_vnd += income
                    total_expense_vnd += expense
                elif currency == 'USD' or currency == 'USDT':
                    rate = usdt_rate if currency == 'USDT' else 25000  # USD tạm tính 25000
                    total_income_vnd += income * rate
                    total_expense_vnd += expense * rate
                
                balance_data.append({
                    'currency': currency,
                    'income': income,
                    'expense': expense,
                    'balance': balance,
                    'status': 'positive' if balance > 0 else 'negative' if balance < 0 else 'zero'
                })
            
            total_balance_vnd = total_income_vnd - total_expense_vnd
            
            return {
                'title': title,
                'period': period,
                'balances': balance_data,
                'total_income_vnd': total_income_vnd,
                'total_expense_vnd': total_expense_vnd,
                'total_balance_vnd': total_balance_vnd,
                'total_balance_status': 'positive' if total_balance_vnd > 0 else 'negative' if total_balance_vnd < 0 else 'zero',
                'income_count': incomes.get('total_count', 0),
                'expense_count': expenses.get('total_count', 0)
            }
        except Exception as e:
            logger.error(f"❌ Lỗi get_balance_summary: {e}")
            return None

    # ==================== HÀM XÓA ====================
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

    def delete_category(category_id, owner_id):
        """
        Xóa danh mục chi tiêu
        category_id: ID của danh mục cần xóa
        owner_id: ID của chủ sở hữu
        Trả về: (success, message, deleted_expenses_count)
        """
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Kiểm tra danh mục có tồn tại và thuộc về owner không
            c.execute('''SELECT id, name FROM expense_categories 
                         WHERE id = ? AND user_id = ?''', (category_id, owner_id))
            category = c.fetchone()
            
            if not category:
                return False, "❌ Không tìm thấy danh mục!", 0
            
            category_name = category[1]
            
            # Đếm số chi tiêu trong danh mục này
            c.execute('''SELECT COUNT(*) FROM expenses 
                         WHERE category_id = ? AND user_id = ?''', (category_id, owner_id))
            expenses_count = c.fetchone()[0]
            
            # Bắt đầu transaction
            c.execute("BEGIN TRANSACTION")
            
            # Xóa tất cả chi tiêu trong danh mục
            c.execute('''DELETE FROM expenses 
                         WHERE category_id = ? AND user_id = ?''', (category_id, owner_id))
            deleted_expenses = c.rowcount
            
            # Xóa danh mục
            c.execute('''DELETE FROM expense_categories 
                         WHERE id = ? AND user_id = ?''', (category_id, owner_id))
            
            conn.commit()
            
            logger.info(f"✅ Đã xóa danh mục {category_name} (ID: {category_id}) của user {owner_id}, kèm {deleted_expenses} khoản chi")
            
            return True, category_name, deleted_expenses
            
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"❌ Lỗi xóa danh mục: {e}")
            return False, str(e), 0
        finally:
            if conn:
                conn.close()

    # ==================== KEYBOARD ====================
    def get_main_keyboard():
        keyboard = [
            [KeyboardButton("💰 ĐẦU TƯ COIN"), KeyboardButton("💸 QUẢN LÝ CHI TIÊU")],
            [KeyboardButton("❓ HƯỚNG DẪN")]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_invest_menu_keyboard(user_id=None, group_id=None):
        keyboard = [
            [InlineKeyboardButton("₿ BTC", callback_data="price_BTC"),
             InlineKeyboardButton("Ξ ETH", callback_data="price_ETH"),
             InlineKeyboardButton("Ξ SOL", callback_data="price_SOL"),
             InlineKeyboardButton("💵 USDT", callback_data="price_USDT")],
            [InlineKeyboardButton("📊 Top 10", callback_data="show_top10"),
             InlineKeyboardButton("👥 Xem danh mục", callback_data="show_portfolio")],
            [InlineKeyboardButton("📈 Lợi nhuận", callback_data="show_profit"),
             InlineKeyboardButton("✏️ Sửa/Xóa", callback_data="edit_transactions")],
            [InlineKeyboardButton("🔔 Cảnh báo giá", callback_data="show_alerts"),
             InlineKeyboardButton("📊 Thống kê", callback_data="show_stats")],
            [InlineKeyboardButton("📥 Xuất CSV", callback_data="export_csv"),
             InlineKeyboardButton("➖ Bán coin", callback_data="show_sell")],
            [InlineKeyboardButton("➕ Mua coin", callback_data="show_buy")]
        ]
        
        if group_id and user_id:
            try:
                if check_permission(group_id, user_id, 'view'):
                    keyboard.append([InlineKeyboardButton("👑 ADMIN", callback_data="admin_panel")])
            except:
                pass
        
        return InlineKeyboardMarkup(keyboard)

    def get_expense_menu_keyboard():
        keyboard = [
            [InlineKeyboardButton("💰 THU NHẬP", callback_data="expense_income_menu"),
             InlineKeyboardButton("💸 CHI TIÊU", callback_data="expense_expense_menu")],
            [InlineKeyboardButton("📋 DANH MỤC", callback_data="expense_categories"),
             InlineKeyboardButton("⚖️ CÂN ĐỐI", callback_data="balance_month")],
            [InlineKeyboardButton("📅 HÔM NAY", callback_data="expense_today"),
             InlineKeyboardButton("📅 THÁNG NÀY", callback_data="expense_month")],
            [InlineKeyboardButton("🔄 GẦN ĐÂY", callback_data="expense_recent"),
             InlineKeyboardButton("📥 XUẤT CSV", callback_data="expense_export")],
            [InlineKeyboardButton("🔙 VỀ MENU CHÍNH", callback_data="back_to_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

    # ==================== COMMAND HANDLERS ====================
    @auto_update_user
    async def whoami_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Kiểm tra thông tin user đã được lưu trong database"""
        user = update.effective_user
        
        # Lấy thông tin từ database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''SELECT user_id, username, first_name, last_name, last_seen 
                     FROM users WHERE user_id = ?''', (user.id,))
        db_user = c.fetchone()
        conn.close()
        
        msg = f"👤 *THÔNG TIN CỦA BẠN*\n━━━━━━━━━━━━━━━━\n\n"
        msg += f"• ID: `{user.id}`\n"
        msg += f"• Username: @{user.username if user.username else 'None'}\n"
        msg += f"• First Name: {user.first_name}\n"
        msg += f"• Last Name: {user.last_name}\n\n"
        
        if db_user:
            msg += f"*📦 DATABASE:*\n"
            msg += f"• Username: @{db_user[1] if db_user[1] else 'None'}\n"
            msg += f"• Last Seen: {db_user[4]}\n"
            msg += f"• Status: ✅ Đã được lưu"
        else:
            msg += f"• Status: ❌ Chưa được lưu trong database"
        
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    @auto_update_user
    async def quick_grant_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Grant quyền nhanh bằng cách reply tin nhắn"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Kiểm tra quyền
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền quản lý phân quyền!")
            return
        
        # Kiểm tra có reply không
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ Hãy reply tin nhắn của người cần grant!")
            return
        
        if not ctx.args:
            await update.message.reply_text("❌ Thiếu loại quyền! VD: `/permgrant view`", parse_mode=ParseMode.MARKDOWN)
            return
        
        target_user = update.message.reply_to_message.from_user
        perm_type = ctx.args[0].lower()
        
        # Cập nhật user info
        await update_user_info_async(target_user)
        
        # Xử lý quyền
        permissions = {'view': 0, 'edit': 0, 'delete': 0, 'manage': 0}
        
        if perm_type == 'view':
            permissions['view'] = 1
        elif perm_type == 'edit':
            permissions['view'] = 1
            permissions['edit'] = 1
        elif perm_type == 'delete':
            permissions['view'] = 1
            permissions['delete'] = 1
        elif perm_type == 'manage':
            permissions['manage'] = 1
        elif perm_type == 'full':
            permissions['view'] = 1
            permissions['edit'] = 1
            permissions['delete'] = 1
            permissions['manage'] = 1
        else:
            await update.message.reply_text("❌ Loại quyền không hợp lệ!")
            return
        
        if grant_permission(chat_id, target_user.id, user_id, permissions):
            await update.message.reply_text(
                f"✅ Đã cấp quyền {perm_type} cho @{target_user.username or target_user.id}"
            )
        else:
            await update.message.reply_text("❌ Lỗi khi cấp quyền!")
    
    @auto_update_user
    async def getid_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Lấy ID của user"""
        user = update.effective_user
        chat = update.effective_chat
        
        msg = f"🔑 *THÔNG TIN ID*\n━━━━━━━━━━━━━━━━\n\n"
        msg += f"👤 *Bạn:*\n"
        msg += f"• ID: `{user.id}`\n"
        msg += f"• Username: @{user.username if user.username else 'None'}\n\n"
        
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            replied = update.message.reply_to_message.from_user
            msg += f"👥 *Người được reply:*\n"
            msg += f"• ID: `{replied.id}`\n"
            msg += f"• Username: @{replied.username if replied.username else 'None'}\n"
        
        msg += f"\n💡 Dùng ID để grant: `/perm grant {user.id} view`"
        
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    @auto_update_user
    async def sync_users_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Đồng bộ danh sách user trong group"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Kiểm tra quyền
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền!")
            return
        
        msg = await update.message.reply_text("🔄 Đang đồng bộ danh sách thành viên...")
        
        try:
            # Lấy danh sách admin group
            admins = await ctx.bot.get_chat_administrators(chat_id)
            count = 0
            
            for admin in admins:
                if admin.user:
                    await update_user_info_async(admin.user)
                    count += 1
            
            await msg.edit_text(
                f"✅ *ĐỒNG BỘ THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n"
                f"📊 Đã cập nhật: {count} admin\n"
                f"👥 Tổng số: {len(admins)} thành viên\n\n"
                f"🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await msg.edit_text(f"❌ Lỗi: {e}")

    @auto_update_user
    async def owner_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Panel quản lý dành cho Owner"""
        user_id = update.effective_user.id
        
        if not is_owner(user_id):
            await update.message.reply_text("❌ Chỉ Owner mới có quyền sử dụng lệnh này!")
            return
        
        if not ctx.args:
            msg = (
                "👑 *OWNER PANEL*\n━━━━━━━━━━━━━━━━\n\n"
                "*QUẢN LÝ NHÂN VIÊN:*\n"
                "• `/owner addstaff @user` - Thêm nhân viên\n"
                "• `/owner removestaff @user` - Xóa nhân viên\n"
                "• `/owner liststaff` - Danh sách nhân viên\n\n"
                "*QUẢN LÝ NGƯỜI DÙNG:*\n"
                "• `/owner approve @user` - Duyệt user\n"
                "• `/owner revoke @user` - Thu hồi quyền\n"
                "• `/owner listpending` - DS chờ duyệt\n"
                "• `/owner listusers` - DS người dùng\n\n"
                "*THỐNG KÊ:*\n"
                "• `/owner stats` - Thống kê hệ thống\n\n"
                f"🕐 {format_vn_time()}"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        action = ctx.args[0].lower()
        
        if action == "addstaff" and len(ctx.args) >= 2:
            target = ctx.args[1]
            target_id = await resolve_user_id(target, ctx)
            
            if not target_id:
                await update.message.reply_text("❌ Không tìm thấy user!")
                return
            
            chat_id = update.effective_chat.id
            
            if grant_user_access(chat_id, target_id, user_id, role='staff'):
                await update.message.reply_text(
                    f"✅ Đã thêm @{target} làm nhân viên!\n"
                    f"Họ có thể quản lý dữ liệu trong group này."
                )
            else:
                await update.message.reply_text("❌ Lỗi khi thêm nhân viên!")

        elif action == "removestaff" and len(ctx.args) >= 2:
            target = ctx.args[1]
            target_id = await resolve_user_id(target, ctx)
            
            if not target_id:
                await update.message.reply_text("❌ Không tìm thấy user!")
                return
            
            chat_id = update.effective_chat.id
            
            # Kiểm tra xem user có phải staff không
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT role FROM permissions 
                         WHERE group_id = ? AND user_id = ?''',
                      (chat_id, target_id))
            result = c.fetchone()
            
            if not result or result[0] != 'staff':
                conn.close()
                await update.message.reply_text(f"❌ {target} không phải là nhân viên!")
                return
            conn.close()
            
            # Xóa quyền staff
            if revoke_permission(chat_id, target_id):
                await update.message.reply_text(
                    f"✅ Đã xóa @{target} khỏi danh sách nhân viên!"
                )
            else:
                await update.message.reply_text("❌ Lỗi khi xóa nhân viên!")

        elif action == "liststaff":
            chat_id = update.effective_chat.id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT p.user_id, p.can_view_all, p.can_edit_all, p.can_delete_all, 
                                p.can_manage_perms, u.username, u.first_name
                         FROM permissions p
                         LEFT JOIN users u ON p.user_id = u.user_id
                         WHERE p.group_id = ? AND p.role = 'staff'
                         ORDER BY p.created_at''', (chat_id,))
            staff_list = c.fetchall()
            conn.close()
            
            if not staff_list:
                await update.message.reply_text("📭 Chưa có nhân viên nào!")
                return
            
            msg = "👥 *DANH SÁCH NHÂN VIÊN*\n━━━━━━━━━━━━━━━━\n\n"
            for staff in staff_list:
                user_id, view, edit, delete, manage, username, first_name = staff
                
                # Tạo tên hiển thị
                if username:
                    display = f"`{user_id}` @{username}"
                elif first_name:
                    display = f"`{user_id}` {first_name}"
                else:
                    display = f"`{user_id}`"
                
                # Liệt kê quyền
                permissions = []
                if view: permissions.append("👁 Xem")
                if edit: permissions.append("✏️ Sửa")
                if delete: permissions.append("🗑 Xóa")
                if manage: permissions.append("🔐 Quản lý")
                
                msg += f"• {display}: {', '.join(permissions)}\n"
            
            msg += f"\n🕐 {format_vn_time()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

        elif action == "revoke" and len(ctx.args) >= 2:
            target = ctx.args[1]
            target_id = await resolve_user_id(target, ctx)
            
            if not target_id:
                await update.message.reply_text("❌ Không tìm thấy user!")
                return
            
            chat_id = update.effective_chat.id
            
            # Kiểm tra xem user có quyền không
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT role FROM permissions 
                         WHERE group_id = ? AND user_id = ?''',
                      (chat_id, target_id))
            result = c.fetchone()
            conn.close()
            
            if not result:
                await update.message.reply_text(f"❌ {target} chưa được cấp quyền!")
                return
            
            # Không cho phép revoke chính mình
            if target_id == user_id:
                await update.message.reply_text("❌ Không thể tự thu hồi quyền của chính mình!")
                return
            
            # Xóa toàn bộ quyền
            if revoke_permission(chat_id, target_id):
                await update.message.reply_text(
                    f"✅ Đã thu hồi toàn bộ quyền của {target}!"
                )
            else:
                await update.message.reply_text("❌ Lỗi khi thu hồi quyền!")
        
        elif action == "approve" and len(ctx.args) >= 2:
            target = ctx.args[1]
            target_id = await resolve_user_id(target, ctx)
            
            if not target_id:
                await update.message.reply_text("❌ Không tìm thấy user!")
                return
            
            chat_id = update.effective_chat.id
            
            if grant_user_access(chat_id, target_id, user_id, role='user'):
                await update.message.reply_text(
                    f"✅ Đã duyệt @{target} sử dụng bot!\n"
                    f"Họ có thể xem dữ liệu trong group này."
                )
            else:
                await update.message.reply_text("❌ Lỗi khi duyệt user!")
        
        elif action == "listpending":
            chat_id = update.effective_chat.id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT user_id, username, first_name, created_at 
                         FROM permissions 
                         WHERE group_id = ? AND is_approved = 0 AND role = 'user'
                         ORDER BY created_at''', (chat_id,))
            pending = c.fetchall()
            conn.close()
            
            if not pending:
                await update.message.reply_text("📭 Không có user nào chờ duyệt!")
                return
            
            msg = "⏳ *DANH SÁCH CHỜ DUYỆT*\n━━━━━━━━━━━━━━━━\n\n"
            for user in pending:
                user_id, username, first_name, created = user
                display = f"@{username}" if username else first_name or f"User {user_id}"
                msg += f"• {display} (`{user_id}`) - {created[:10]}\n"
            
            msg += f"\n🕐 {format_vn_time()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        
        elif action == "stats":
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            c.execute("SELECT COUNT(DISTINCT user_id) FROM users")
            total_users = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM permissions WHERE role = 'staff'")
            total_staff = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM permissions WHERE role = 'user' AND is_approved = 1")
            total_approved = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM permissions WHERE role = 'user' AND is_approved = 0")
            total_pending = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM portfolio")
            total_transactions = c.fetchone()[0]
            
            c.execute("SELECT COUNT(DISTINCT user_id) FROM portfolio")
            users_with_portfolio = c.fetchone()[0]
            
            conn.close()
            
            msg = (
                "📊 *THỐNG KÊ HỆ THỐNG*\n━━━━━━━━━━━━━━━━\n\n"
                f"👥 *Tổng user:* {total_users}\n"
                f"👑 *Nhân viên:* {total_staff}\n"
                f"✅ *Đã duyệt:* {total_approved}\n"
                f"⏳ *Chờ duyệt:* {total_pending}\n\n"
                f"💼 *Giao dịch:* {total_transactions}\n"
                f"👤 *User có portfolio:* {users_with_portfolio}\n\n"
                f"🕐 {format_vn_time()}"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    async def resolve_user_id(target, ctx):
        """Helper để lấy user ID từ username hoặc reply"""
        if target.startswith('@'):
            username = target[1:]
            return get_user_id_by_username(username)
        else:
            try:
                return int(target)
            except:
                if ctx.message.reply_to_message:
                    return ctx.message.reply_to_message.from_user.id
        return None

    @auto_update_user
    async def balance_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xem cân đối thu chi"""
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        # Kiểm tra quyền nếu trong group
        if chat_type in ['group', 'supergroup']:
            current_user = update.effective_user.id
            if current_user != user_id and not check_permission(chat_id, current_user, 'view'):
                await update.message.reply_text("❌ Bạn không có quyền xem dữ liệu!")
                return
        
        # Xác định kỳ xem
        period = 'month'  # mặc định
        if ctx.args:
            arg = ctx.args[0].lower()
            if arg in ['day', 'ngay', 'hôm nay', 'today', 'd']:
                period = 'day'
            elif arg in ['month', 'thang', 'tháng', 'this month', 'm']:
                period = 'month'
            elif arg in ['year', 'nam', 'năm', 'this year', 'y']:
                period = 'year'
            elif arg in ['all', 'tat ca', 'tất cả', 'all time', 'a']:
                period = 'all'
        
        msg = await update.message.reply_text("🔄 Đang tính toán cân đối...")
        
        # Lấy thông tin user
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (user_id,))
        user_info = c.fetchone()
        conn.close()
        
        user_name = f"@{user_info[0]}" if user_info and user_info[0] else (user_info[1] if user_info else "")
        
        # Tính cân đối
        balance_data = get_balance_summary(user_id, period)
        
        if not balance_data:
            await msg.edit_text("❌ Không thể tính cân đối!")
            return
        
        # Format và gửi
        balance_msg = format_balance_message(balance_data, user_name)
        
        # Thêm keyboard
        keyboard = [
            [InlineKeyboardButton("📅 Hôm nay", callback_data="balance_day"),
             InlineKeyboardButton("📅 Tháng này", callback_data="balance_month")],
            [InlineKeyboardButton("📅 Năm nay", callback_data="balance_year"),
             InlineKeyboardButton("📊 Tất cả", callback_data="balance_all")],
            [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]
        ]
        
        await msg.edit_text(
            balance_msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    @auto_update_user
    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type in ['group', 'supergroup']:
            welcome_msg = (
                "🚀 *ĐẦU TƯ COIN & QUẢN LÝ CHI TIÊU*\n\n"
                "🤖 Bot đã sẵn sàng!\n\n"
                "*Các lệnh trong nhóm:*\n"
                "• `/s btc eth` - Xem giá coin\n"
                "• `/usdt` - Tỷ giá USDT/VND\n"
                "• `/buy btc 0.5 40000` - Mua coin\n"
                "• `/sell btc 0.2` - Bán coin\n\n"
                "📱 *Vuốt xuống để hiện menu*\n"
                f"🕐 {format_vn_time()}"
            )
            await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())
        else:
            welcome_msg = (
                "🚀 *ĐẦU TƯ COIN & QUẢN LÝ CHI TIÊU*\n\n"
                "🤖 Bot hỗ trợ:\n\n"
                "*💎 ĐẦU TƯ COIN:*\n"
                "• Xem giá coin\n• Top 10 coin\n• Quản lý danh mục\n• Tính lợi nhuận\n• Cảnh báo giá\n\n"
                "*💰 QUẢN LÝ CHI TIÊU:*\n"
                "• Ghi chép thu/chi\n• Đa tiền tệ\n• Quản lý ngân sách\n• Báo cáo ngày/tháng/năm\n\n"
                f"🕐 *Hiện tại:* `{format_vn_time()}`\n\n"
                "👇 *Chọn chức năng bên dưới*"
            )
            await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())

    @auto_update_user
    async def menu_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👇 *Chọn chức năng bên dưới*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard()
        )

    @auto_update_user
    async def hide_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Ẩn bàn phím"""
        await update.message.reply_text(
            "✅ Đã ẩn bàn phím. Gõ /menu để hiện lại.",
            reply_markup=ReplyKeyboardRemove()
        )

    @auto_update_user
    async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        help_msg = (
            "📘 *HƯỚNG DẪN*\n\n"
        )
        
        if is_owner(user_id):
            help_msg += (
                "*👑 OWNER COMMANDS:*\n"
                "• `/owner` - Panel quản lý\n"
                "• `/owner addstaff @user` - Thêm nhân viên\n"
                "• `/owner approve @user` - Duyệt user\n"
                "• `/owner listpending` - DS chờ duyệt\n\n"
            )
        
        # Kiểm tra quyền user
        if check_user_access(chat_id, user_id, 'user'):
            help_msg += (
                "*ĐẦU TƯ COIN:*\n"
                "• `/s btc eth` - Xem giá coin\n"
                "• `/usdt` - Tỷ giá USDT/VND\n"
                "• `/buy btc 0.5 40000` - Mua coin\n"
                "• `/sell btc 0.2` - Bán coin\n\n"
            )
        
        # Kiểm tra quyền staff
        if check_user_access(chat_id, user_id, 'staff'):
            help_msg += (
                "*👥 NHÂN VIÊN:*\n"
                "• `/edit` - Sửa giao dịch\n"
                "• `/del` - Xóa giao dịch\n"
                "• `/view @user` - Xem portfolio người khác\n\n"
            )
        
        help_msg += f"\n🕐 {format_vn_time()}"
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
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

    @auto_update_user
    @require_permission('user')
    async def s_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await update.message.reply_text("❌ /s btc eth doge")
        
        msg = await update.message.reply_text("🔄 Đang tra cứu...")
        
        # Lấy giá batch
        symbols = [arg.upper() for arg in ctx.args]
        prices = get_prices_batch(symbols)
        
        results = []
        for symbol in symbols:
            d = prices.get(symbol)
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

    @auto_update_user
    @require_permission('user')
    async def buy_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Lấy owner_id từ context (đã được set bởi auto_update_user)
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        if len(ctx.args) < 3:
            return await update.message.reply_text("❌ /buy btc 0.5 40000")
        
        symbol = ctx.args[0].upper()
        
        try:
            amount = float(ctx.args[1])
            buy_price = float(ctx.args[2])
        except ValueError:
            return await update.message.reply_text("❌ Số lượng/giá không hợp lệ!")
        
        if amount <= 0 or buy_price <= 0:
            return await update.message.reply_text("❌ Số lượng và giá phải > 0")
        
        price_data = get_price(symbol)
        if not price_data:
            return await update.message.reply_text(f"❌ Không thể lấy giá *{symbol}*", parse_mode='Markdown')
        
        # QUAN TRỌNG: Dùng owner_id thay vì user_id của người gửi
        if add_transaction(owner_id, symbol, amount, buy_price):
            current_price = price_data['p']
            profit = (current_price - buy_price) * amount
            profit_percent = ((current_price - buy_price) / buy_price) * 100
            
            # Thêm thông báo ai đã thêm
            added_by = f" (thêm bởi @{update.effective_user.username})" if update.effective_user.username else ""
            
            msg = (
                f"✅ *ĐÃ MUA {symbol}*{added_by}\n━━━━━━━━━━━━━━━━\n\n"
                f"📊 SL: `{amount:.4f}`\n"
                f"💰 Giá mua: `{fmt_price(buy_price)}`\n"
                f"💵 Vốn: `{fmt_price(amount * buy_price)}`\n"
                f"📈 Giá hiện: `{fmt_price(current_price)}`\n"
                f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n"
                f"🕐 {format_vn_time()}"
            )
            await update.message.reply_text(msg, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Lỗi khi thêm giao dịch *{symbol}*", parse_mode='Markdown')

    @auto_update_user
    @require_permission('user')
    async def sell_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Lấy owner_id từ context
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        if len(ctx.args) < 2:
            return await update.message.reply_text("❌ /sell btc 0.2")
        
        symbol = ctx.args[0].upper()
        
        try:
            sell_amount = float(ctx.args[1])
        except ValueError:
            return await update.message.reply_text("❌ Số lượng không hợp lệ!")
        
        if sell_amount <= 0:
            return await update.message.reply_text("❌ Số lượng phải > 0")
        
        # Lấy portfolio của OWNER
        portfolio_data = get_portfolio(owner_id)
        if not portfolio_data:
            return await update.message.reply_text("📭 Danh mục trống!")
        
        # Xử lý bán (giữ nguyên logic cũ, nhưng dùng owner_id)
        portfolio = []
        for row in portfolio_data:
            portfolio.append({'symbol': row[0], 'amount': row[1], 'buy_price': row[2], 'buy_date': row[3], 'total_cost': row[4]})
        
        symbol_txs = [tx for tx in portfolio if tx['symbol'] == symbol]
        if not symbol_txs:
            return await update.message.reply_text(f"❌ Không có *{symbol}*", parse_mode='Markdown')
        
        total_amount = sum(tx['amount'] for tx in symbol_txs)
        if sell_amount > total_amount:
            return await update.message.reply_text(f"❌ Chỉ có {total_amount:.4f} {symbol}")
        
        price_data = get_price(symbol)
        if not price_data:
            return await update.message.reply_text(f"❌ Không thể lấy giá *{symbol}*", parse_mode='Markdown')
        
        current_price = price_data['p']
        
        remaining_sell = sell_amount
        new_portfolio = []
        sold_value = 0
        sold_cost = 0
        
        for tx in portfolio:
            if tx['symbol'] == symbol and remaining_sell > 0:
                if tx['amount'] <= remaining_sell:
                    sold_cost += tx['total_cost']
                    sold_value += tx['amount'] * current_price
                    remaining_sell -= tx['amount']
                else:
                    sell_part = remaining_sell
                    sold_cost += sell_part * tx['buy_price']
                    sold_value += sell_part * current_price
                    tx['amount'] -= sell_part
                    tx['total_cost'] = tx['amount'] * tx['buy_price']
                    new_portfolio.append(tx)
                    remaining_sell = 0
            else:
                new_portfolio.append(tx)
        
        # Cập nhật database cho OWNER
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM portfolio WHERE user_id = ?", (owner_id,))
        for tx in new_portfolio:
            c.execute('''INSERT INTO portfolio (user_id, symbol, amount, buy_price, buy_date, total_cost)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (owner_id, tx['symbol'], tx['amount'], tx['buy_price'], tx['buy_date'], tx['total_cost']))
        conn.commit()
        conn.close()
        
        profit = sold_value - sold_cost
        profit_percent = (profit / sold_cost) * 100 if sold_cost > 0 else 0
        
        sold_by = f" (bán bởi @{update.effective_user.username})" if update.effective_user.username else ""
        
        msg = (
            f"✅ *ĐÃ BÁN {sell_amount:.4f} {symbol}*{sold_by}\n━━━━━━━━━━━━━━━━\n\n"
            f"💰 Giá bán: `{fmt_price(current_price)}`\n"
            f"💵 Giá trị: `{fmt_price(sold_value)}`\n"
            f"📊 Vốn: `{fmt_price(sold_cost)}`\n"
            f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n"
            f"🕐 {format_vn_time()}"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

    @auto_update_user
    @require_permission('staff')
    async def edit_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = ctx.bot_data.get('effective_user_id', update.effective_user.id)
    
        if not ctx.args:
            transactions = get_transaction_detail(uid)
            if not transactions:
                await update.message.reply_text("📭 Danh mục trống!")
                return
    
            msg = "📝 *CHỌN GIAO DỊCH*\n━━━━━━━━━━━━\n\n"
            keyboard = []
            row = []
    
            for i, tx in enumerate(transactions, 1):
                tx_id, symbol, amount, price, date, total = tx
                short_date = date.split()[0]
                msg += f"*{i}.* {symbol} - {amount:.4f} @ {fmt_price(price)} - {short_date}\n"
    
                row.append(InlineKeyboardButton(f"✏️ #{tx_id}", callback_data=f"edit_{tx_id}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
    
            if row:
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")])
    
            msg += f"\n🕐 {format_vn_time_short()}"
    
            await update.message.reply_text(
                msg, parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
    
        if len(ctx.args) == 1:
            try:
                tx_id = int(ctx.args[0])
                transactions = get_transaction_detail(uid)
                
                tx = next((t for t in transactions if t[0] == tx_id), None)
                if not tx:
                    await update.message.reply_text(f"❌ Không tìm thấy giao dịch #{tx_id}")
                    return
                
                tx_id, symbol, amount, price, date, total = tx
                price_data = get_price(symbol)
                current_price = price_data['p'] if price_data else 0
                profit = (current_price - price) * amount if current_price else 0
                profit_percent = ((current_price - price) / price) * 100 if price and current_price else 0
                
                msg = (
                    f"📝 *GIAO DỊCH #{tx_id}*\n━━━━━━━━━━━━━━━━\n\n"
                    f"*{symbol}*\n📅 {date}\n📊 SL: `{amount:.4f}`\n"
                    f"💰 Giá mua: `{fmt_price(price)}`\n💵 Vốn: `{fmt_price(total)}`\n"
                    f"📈 Giá hiện: `{fmt_price(current_price)}`\n"
                    f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n"
                    f"*Sửa:* `/edit {tx_id} [sl] [giá]`\n*Xóa:* `/del {tx_id}`\n\n"
                    f"🕐 {format_vn_time()}"
                )
                
                keyboard = [[
                    InlineKeyboardButton("✏️ Sửa", callback_data=f"edit_{tx_id}"),
                    InlineKeyboardButton("🗑 Xóa", callback_data=f"del_{tx_id}")
                ],[
                    InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")
                ]]
                
                await update.message.reply_text(
                    msg, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except ValueError:
                await update.message.reply_text("❌ ID không hợp lệ")
        
        elif len(ctx.args) == 3:
            try:
                tx_id = int(ctx.args[0])
                new_amount = float(ctx.args[1])
                new_price = float(ctx.args[2])
                
                if new_amount <= 0 or new_price <= 0:
                    await update.message.reply_text("❌ SL và giá phải > 0")
                    return
                
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                new_total = new_amount * new_price
                c.execute('''UPDATE portfolio SET amount = ?, buy_price = ?, total_cost = ?
                             WHERE id = ? AND user_id = ?''',
                          (new_amount, new_price, new_total, tx_id, uid))
                conn.commit()
                affected = c.rowcount
                conn.close()
                
                if affected > 0:
                    await update.message.reply_text(
                        f"✅ Đã cập nhật giao dịch #{tx_id}\n"
                        f"📊 SL mới: `{new_amount:.4f}`\n"
                        f"💰 Giá mới: `{fmt_price(new_price)}`\n\n"
                        f"🕐 {format_vn_time()}",
                        parse_mode='Markdown'
                    )
                else:
                    await update.message.reply_text(f"❌ Không tìm thấy giao dịch #{tx_id}")
            except ValueError:
                await update.message.reply_text("❌ /edit [id] [sl] [giá]")
        else:
            await update.message.reply_text("❌ /edit - Xem DS\n/edit [id] - Xem chi tiết\n/edit [id] [sl] [giá] - Sửa")

    @auto_update_user
    @require_permission('staff')
    async def delete_tx_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = ctx.bot_data.get('effective_user_id', update.effective_user.id)
    
        if not ctx.args:
            await update.message.reply_text("❌ /del [id]")
            return
    
        try:
            tx_id = int(ctx.args[0])
    
            keyboard = [[
                InlineKeyboardButton("✅ Có", callback_data=f"confirm_del_{tx_id}"),
                InlineKeyboardButton("❌ Không", callback_data="show_portfolio")
            ]]
    
            await update.message.reply_text(
                f"⚠️ *Xác nhận xóa giao dịch #{tx_id}?*\n\n🕐 {format_vn_time_short()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except ValueError:
            await update.message.reply_text("❌ ID không hợp lệ")

    @auto_update_user
    @rate_limit(30)
    async def alert_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Lấy owner_id
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        if len(ctx.args) < 3:
            await update.message.reply_text("❌ /alert BTC above 50000", parse_mode='Markdown')
            return
        
        symbol = ctx.args[0].upper()
        condition = ctx.args[1].lower()
        try:
            target_price = float(ctx.args[2])
        except ValueError:
            return await update.message.reply_text("❌ Giá không hợp lệ!")
        
        if condition not in ['above', 'below']:
            return await update.message.reply_text("❌ Điều kiện phải là 'above' hoặc 'below'")
        
        price_data = get_price(symbol)
        if not price_data:
            return await update.message.reply_text(f"❌ Không tìm thấy coin *{symbol}*", parse_mode='Markdown')
        
        # Dùng owner_id thay vì user_id
        if add_alert(owner_id, symbol, target_price, condition):
            msg = (
                f"✅ *ĐÃ TẠO CẢNH BÁO*\n━━━━━━━━━━━━━━━━\n\n"
                f"• Coin: *{symbol}*\n"
                f"• Mốc giá: `{fmt_price(target_price)}`\n"
                f"• Giá hiện tại: `{fmt_price(price_data['p'])}`\n"
                f"• Điều kiện: {'📈 Lên trên' if condition == 'above' else '📉 Xuống dưới'}\n\n"
                f"🕐 {format_vn_time()}"
            )
            await update.message.reply_text(msg, parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Lỗi khi tạo cảnh báo!")

    @auto_update_user
    @rate_limit(30)
    async def alerts_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        alerts = get_user_alerts(uid)
        
        if not alerts:
            await update.message.reply_text("📭 Bạn chưa có cảnh báo nào!")
            return
        
        msg = "🔔 *DANH SÁCH CẢNH BÁO*\n━━━━━━━━━━━━━━━━\n\n"
        for alert in alerts:
            alert_id, symbol, target, condition, created = alert
            created_date = created.split()[0]
            price_data = get_price(symbol)
            current_price = price_data['p'] if price_data else 0
            status = "🟢" if (condition == 'above' and current_price < target) or (condition == 'below' and current_price > target) else "🔴"
            msg += f"{status} *#{alert_id}*: {symbol} {condition} `{fmt_price(target)}`\n"
            msg += f"   Giá hiện: `{fmt_price(current_price)}` (tạo {created_date})\n\n"
        
        msg += f"🕐 {format_vn_time_short()}"
        await update.message.reply_text(msg, parse_mode='Markdown')

    @auto_update_user
    @rate_limit(30)
    async def stats_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        msg = await update.message.reply_text("🔄 Đang tính toán thống kê...")
        
        stats = get_portfolio_stats(uid)
        
        if not stats:
            await msg.edit_text("📭 Danh mục trống!")
            return
        
        stats_msg = (
            f"📊 *THỐNG KÊ DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
            f"*TỔNG QUAN*\n"
            f"• Vốn: `{fmt_price(stats['total_invest'])}`\n"
            f"• Giá trị: `{fmt_price(stats['total_value'])}`\n"
            f"• Lợi nhuận: `{fmt_price(stats['total_profit'])}`\n"
            f"• Tỷ suất: `{stats['total_profit_percent']:+.2f}%`\n\n"
            f"*📈 TOP COIN LỜI NHẤT*\n"
        )
        
        count = 0
        for symbol, profit, profit_pct, value, cost in stats['coin_profits']:
            if profit > 0:
                count += 1
                stats_msg += f"{count}. *{symbol}*: `{fmt_price(profit)}` ({profit_pct:+.2f}%)\n"
            if count >= 3:
                break
        
        if count == 0:
            stats_msg += "Không có coin lời\n"
        
        stats_msg += f"\n*📉 TOP COIN LỖ NHẤT*\n"
        count = 0
        for symbol, profit, profit_pct, value, cost in reversed(stats['coin_profits']):
            if profit < 0:
                count += 1
                stats_msg += f"{count}. *{symbol}*: `{fmt_price(profit)}` ({profit_pct:+.2f}%)\n"
            if count >= 3:
                break
        
        if count == 0:
            stats_msg += "Không có coin lỗ\n"
        
        stats_msg += f"\n🕐 {format_vn_time()}"
        
        await msg.edit_text(stats_msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
    async def view_portfolio_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xem portfolio của user khác (dành cho admin)"""
        user_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Kiểm tra quyền xem
        if not check_permission(chat_id, user_id, 'view'):
            await update.message.reply_text("❌ Bạn không có quyền xem dữ liệu!")
            return
        
        if not ctx.args:
            await update.message.reply_text("❌ /view [@username hoặc ID]")
            return
        
        target = ctx.args[0]
        target_user_id = None
        
        # Xác định user cần xem
        if target.startswith('@'):
            username = target[1:]
            target_user_id = get_user_id_by_username(username)
        else:
            try:
                target_user_id = int(target)
            except:
                pass
        
        if not target_user_id:
            await update.message.reply_text(f"❌ Không tìm thấy user {target}")
            return
        
        # Lấy portfolio của user đó
        portfolio_data = get_portfolio(target_user_id)
        
        if not portfolio_data:
            await update.message.reply_text(f"📭 Danh mục của {target} trống!")
            return
        
        # Lấy thông tin user
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
        user_info = c.fetchone()
        conn.close()
        
        display_name = user_info[0] if user_info and user_info[0] else f"User {target_user_id}"
        
        # Lấy giá batch
        symbols = list(set([row[0] for row in portfolio_data]))
        prices = get_prices_batch(symbols)
        
        summary = {}
        total_invest = 0
        total_value = 0
        
        for row in portfolio_data:
            symbol, amount, price, date, cost = row
            if symbol not in summary:
                summary[symbol] = {'amount': 0, 'cost': 0}
            summary[symbol]['amount'] += amount
            summary[symbol]['cost'] += cost
            total_invest += cost
        
        msg = f"📊 *DANH MỤC CỦA {display_name}*\n━━━━━━━━━━━━\n\n"
        
        for symbol, data in summary.items():
            price_data = prices.get(symbol)
            if price_data:
                current = data['amount'] * price_data['p']
                profit = current - data['cost']
                profit_percent = (profit / data['cost']) * 100 if data['cost'] > 0 else 0
                total_value += current
                
                msg += f"*{symbol}*\n"
                msg += f"📊 SL: `{data['amount']:.4f}`\n"
                msg += f"💰 TB: `{fmt_price(data['cost']/data['amount'])}`\n"
                msg += f"💎 TT: `{fmt_price(current)}`\n"
                msg += f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n"
        
        total_profit = total_value - total_invest
        total_profit_percent = (total_profit / total_invest) * 100 if total_invest > 0 else 0
        
        msg += "━━━━━━━━━━━━\n"
        msg += f"💵 Vốn: `{fmt_price(total_invest)}`\n"
        msg += f"💰 GT: `{fmt_price(total_value)}`\n"
        msg += f"{'✅' if total_profit>=0 else '❌'} Tổng LN: `{fmt_price(total_profit)}` ({total_profit_percent:+.2f}%)\n\n"
        msg += f"🕐 {format_vn_time()}"
        
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
    async def list_users_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xem danh sách user trong group (dành cho admin)"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Kiểm tra quyền xem
        if not check_permission(chat_id, user_id, 'view'):
            await update.message.reply_text("❌ Bạn không có quyền xem danh sách!")
            return
        
        try:
            admins = await ctx.bot.get_chat_administrators(chat_id)
            
            msg = "👥 *THÀNH VIÊN TRONG NHÓM*\n━━━━━━━━━━━━━━━━\n\n"
            
            for admin in admins:
                user = admin.user
                status = "👑 Admin" if admin.status in ['administrator', 'creator'] else "👤 Member"
                
                # Lấy thông tin từ database
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT last_seen FROM users WHERE user_id = ?", (user.id,))
                db_user = c.fetchone()
                conn.close()
                
                last_seen = db_user[0][:10] if db_user else "Chưa từng"
                
                msg += f"• {status}\n"
                msg += f"  ID: `{user.id}`\n"
                msg += f"  Username: @{user.username if user.username else 'None'}\n"
                msg += f"  Tên: {user.first_name} {user.last_name or ''}\n"
                msg += f"  Lần cuối: {last_seen}\n\n"
            
            msg += f"🕐 {format_vn_time()}"
            
            # Gửi từng phần nếu quá dài
            if len(msg) > 4000:
                chunks = [msg[i:i+4000] for i in range(0, len(msg), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
                
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: {e}")

    @auto_update_user
    async def sync_admins_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Đồng bộ và tự động cấp quyền cho tất cả admin trong group"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Kiểm tra quyền manage
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này!")
            return
        
        msg = await update.message.reply_text("🔄 Đang đồng bộ danh sách admin...")
        
        try:
            # Lấy danh sách admin từ Telegram
            admins = await ctx.bot.get_chat_administrators(chat_id)
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            granted_count = 0
            updated_count = 0
            
            for admin in admins:
                if admin.user:
                    # Cập nhật user info
                    await update_user_info_async(admin.user)
                    
                    # Kiểm tra xem đã có quyền chưa
                    c.execute("SELECT * FROM permissions WHERE group_id = ? AND user_id = ?", 
                              (chat_id, admin.user.id))
                    exists = c.fetchone()
                    
                    if not exists:
                        # Nếu chưa có, cấp quyền
                        permissions = {'view': 1, 'edit': 0, 'delete': 0, 'manage': 0}
                        role = 'user'
                        
                        # Nếu là creator hoặc admin thì cấp quyền cao hơn
                        if admin.status == 'creator':
                            permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 1}
                            role = 'staff'
                        elif admin.status == 'administrator':
                            permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 0}
                            role = 'staff'
                        
                        created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
                        c.execute('''INSERT INTO permissions 
                                     (group_id, user_id, granted_by, is_approved, role,
                                      can_view_all, can_edit_all, can_delete_all, can_manage_perms,
                                      created_at, approved_at)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (chat_id, admin.user.id, user_id,
                                   1, role,
                                   permissions['view'], permissions['edit'], 
                                   permissions['delete'], permissions['manage'],
                                   created_at, created_at))
                        granted_count += 1
                    else:
                        updated_count += 1
            
            conn.commit()
            conn.close()
            
            await msg.edit_text(
                f"✅ *ĐỒNG BỘ ADMIN THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n"
                f"📊 Kết quả:\n"
                f"• Tổng số admin trong group: {len(admins)}\n"
                f"• Đã cấp quyền mới: {granted_count}\n"
                f"• Đã cập nhật: {updated_count}\n\n"
                f"🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            await msg.edit_text(f"❌ Lỗi: {e}")

    @auto_update_user
    async def new_chat_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xử lý khi có thành viên mới vào group"""
        for new_member in update.message.new_chat_members:
            # Cập nhật user info
            await update_user_info_async(new_member)
            
            # Nếu là bot thì không cần xử lý
            if new_member.is_bot:
                continue
            
            chat_id = update.effective_chat.id
            
            # Kiểm tra xem người này có phải là admin không
            try:
                admins = await ctx.bot.get_chat_administrators(chat_id)
                for admin in admins:
                    if admin.user.id == new_member.id:
                        # Nếu là admin, tự động cấp quyền cơ bản
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        
                        # Kiểm tra xem đã có quyền chưa
                        c.execute("SELECT * FROM permissions WHERE group_id = ? AND user_id = ?", 
                                  (chat_id, new_member.id))
                        exists = c.fetchone()
                        
                        if not exists:
                            # Cấp quyền view cơ bản
                            permissions = {'view': 1, 'edit': 0, 'delete': 0, 'manage': 0}
                            
                            # Nếu là creator thì full quyền
                            if admin.status == 'creator':
                                permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 1}
                            
                            c.execute('''INSERT INTO permissions 
                                         (group_id, user_id, granted_by, can_view_all, can_edit_all, 
                                          can_delete_all, can_manage_perms, created_at)
                                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                                      (chat_id, new_member.id, new_member.id,
                                       permissions['view'], permissions['edit'], 
                                       permissions['delete'], permissions['manage'],
                                       get_vn_time().strftime("%Y-%m-%d %H:%M:%S")))
                            conn.commit()
                            
                            logger.info(f"✅ Auto-granted permissions for new admin @{new_member.username} in {chat_id}")
                        
                        conn.close()
                        break
            except Exception as e:
                logger.error(f"❌ Lỗi xử lý new member: {e}")

    @auto_update_user
    async def check_perm_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Kiểm tra quyền của user trong group"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        target_id = user_id
        target_name = "bạn"
        
        # Nếu có reply, kiểm tra người được reply
        if update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
            target_name = f"@{update.message.reply_to_message.from_user.username or target_id}"
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # ĐÃ SỬA: dùng user_id thay vì admin_id
        c.execute('''SELECT can_view_all, can_edit_all, can_delete_all, can_manage_perms 
                     FROM permissions WHERE group_id = ? AND user_id = ?''',
                  (chat_id, target_id))
        result = c.fetchone()
        conn.close()
        
        if not result:
            msg = f"❌ *{target_name}* chưa được cấp quyền trong group này!"
        else:
            can_view, can_edit, can_delete, can_manage = result
            msg = f"🔐 *QUYỀN CỦA {target_name}*\n━━━━━━━━━━━━━━━━\n\n"
            msg += f"• 👁 Xem: {'✅' if can_view else '❌'}\n"
            msg += f"• ✏️ Sửa: {'✅' if can_edit else '❌'}\n"
            msg += f"• 🗑 Xóa: {'✅' if can_delete else '❌'}\n"
            msg += f"• 🔐 Quản lý: {'✅' if can_manage else '❌'}\n"
        
        msg += f"\n🕐 {format_vn_time()}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
    async def sync_data_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Đồng bộ dữ liệu của tất cả user (admin only)"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này!")
            return
        
        msg = await update.message.reply_text("🔄 Đang đồng bộ dữ liệu user...")
        
        try:
            # Lấy danh sách thành viên trong group
            admins = await ctx.bot.get_chat_administrators(chat_id)
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            synced = 0
            for admin in admins:
                if admin.user:
                    # Cập nhật user info
                    current_time = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
                    
                    c.execute('''INSERT OR REPLACE INTO users 
                                 (user_id, username, first_name, last_name, last_seen)
                                 VALUES (?, ?, ?, ?, ?)''',
                              (admin.user.id, 
                               admin.user.username, 
                               admin.user.first_name, 
                               admin.user.last_name, 
                               current_time))
                    synced += 1
            
            conn.commit()
            conn.close()
            
            # Xóa cache username
            username_cache.clear()
            
            await msg.edit_text(
                f"✅ *ĐỒNG BỘ DỮ LIỆU THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n"
                f"📊 Đã đồng bộ: {synced} user\n"
                f"💾 Cache đã được làm mới\n\n"
                f"🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            await msg.edit_text(f"❌ Lỗi: {e}")

    @auto_update_user
    async def debug_perm_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Debug permissions - chỉ dành cho owner"""
        user_id = update.effective_user.id
        
        # Chỉ owner mới dùng được
        if not is_owner(user_id):
            await update.message.reply_text("❌ Chỉ Owner mới có quyền sử dụng lệnh này!")
            return
        
        chat_id = update.effective_chat.id
        
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Lấy thông tin về bảng permissions
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='permissions'")
            if not c.fetchone():
                await update.message.reply_text("❌ Bảng permissions chưa được tạo!")
                conn.close()
                return
            
            # Lấy cấu trúc bảng
            c.execute("PRAGMA table_info(permissions)")
            columns = c.fetchall()
            
            msg = "🔧 *DEBUG PERMISSIONS*\n"
            msg += f"Group ID: `{chat_id}`\n"
            msg += f"User ID: `{user_id}`\n"
            msg += "━━━━━━━━━━━━━━━━\n\n"
            
            msg += "*CẤU TRÚC BẢNG:*\n"
            for col in columns:
                msg += f"• `{col[1]}` ({col[2]})"
                if col[5] == 1:
                    msg += " PRIMARY KEY"
                if col[3] == 1:
                    msg += " NOT NULL"
                if col[4] is not None:
                    msg += f" DEFAULT '{col[4]}'"
                msg += "\n"
            
            # Lấy dữ liệu của group hiện tại
            c.execute("SELECT * FROM permissions WHERE group_id = ?", (chat_id,))
            rows = c.fetchall()
            
            msg += f"\n*DỮ LIỆU ({len(rows)} rows):*\n"
            if rows:
                for row in rows:
                    msg += f"• `{row}`\n"
            else:
                msg += "• Không có dữ liệu\n"
            
            # Kiểm tra quyền của user hiện tại
            c.execute("SELECT * FROM permissions WHERE group_id = ? AND user_id = ?", (chat_id, user_id))
            user_perm = c.fetchone()
            
            msg += f"\n*QUYỀN CỦA BẠN:*\n"
            if user_perm:
                msg += f"• {user_perm}\n"
            else:
                msg += "• Chưa có quyền trong group này\n"
            
            conn.close()
            
            # Gửi từng phần nếu quá dài
            if len(msg) > 4000:
                chunks = [msg[i:i+4000] for i in range(0, len(msg), 4000)]
                for i, chunk in enumerate(chunks, 1):
                    await update.message.reply_text(
                        f"{chunk}\n\n*(Phần {i}/{len(chunks)})*", 
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
                
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: {str(e)}")

    @auto_update_user
    async def setup_group_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Thiết lập group này thuộc về chủ sở hữu"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        # Chỉ owner mới có thể setup
        if user_id != OWNER_ID:
            await update.message.reply_text("❌ Chỉ chủ sở hữu bot mới có thể setup group!")
            return
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong group!")
            return
        
        # Set owner cho group
        if set_group_owner(chat_id, OWNER_ID):
            await update.message.reply_text(
                f"✅ *THIẾT LẬP GROUP THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n"
                f"• Group này đã được đặt dưới quyền sở hữu của bạn\n"
                f"• Tất cả dữ liệu trong group sẽ là của bạn\n"
                f"• Bạn có thể thêm admin để cùng quản lý\n\n"
                f"🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("❌ Lỗi khi thiết lập group!")

    @auto_update_user
    async def group_info_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xem thông tin group"""
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong group!")
            return
        
        owner_id = get_group_owner(chat_id)
        
        # Lấy thông tin owner
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (owner_id,))
        owner_info = c.fetchone()
        conn.close()
        
        owner_display = f"@{owner_info[0]}" if owner_info and owner_info[0] else (owner_info[1] if owner_info else f"User {owner_id}")
        
        msg = (
            f"ℹ️ *THÔNG TIN GROUP*\n━━━━━━━━━━━━━━━━\n\n"
            f"• Group ID: `{chat_id}`\n"
            f"• Chủ sở hữu: {owner_display} (`{owner_id}`)\n"
            f"• Bạn: {update.effective_user.first_name} (`{update.effective_user.id}`)\n\n"
            f"🕐 {format_vn_time()}"
        )
        
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
    async def add_group_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Thêm admin cho group (chỉ chủ sở hữu mới được dùng)"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Chỉ chủ sở hữu group mới được thêm admin
        if not is_group_owner(chat_id, user_id):
            await update.message.reply_text("❌ Chỉ chủ sở hữu group mới có thể thêm admin!")
            return
        
        if not ctx.args:
            await update.message.reply_text("❌ /addadmin @username [view/edit/delete/manage]")
            return
        
        target = ctx.args[0]
        perm_type = ctx.args[1] if len(ctx.args) > 1 else 'view'
        
        target_id = await resolve_user_id(target, ctx)
        if not target_id:
            await update.message.reply_text(f"❌ Không tìm thấy user {target}")
            return
        
        # Xác định quyền
        permissions = {'view': 0, 'edit': 0, 'delete': 0, 'manage': 0}
        
        if perm_type == 'view':
            permissions['view'] = 1
        elif perm_type == 'edit':
            permissions['view'] = 1
            permissions['edit'] = 1
        elif perm_type == 'delete':
            permissions['view'] = 1
            permissions['delete'] = 1
        elif perm_type == 'manage':
            permissions['manage'] = 1
        elif perm_type == 'full':
            permissions['view'] = 1
            permissions['edit'] = 1
            permissions['delete'] = 1
            permissions['manage'] = 1
        else:
            await update.message.reply_text("❌ Loại quyền không hợp lệ!")
            return
        
        if grant_permission(chat_id, target_id, user_id, permissions):
            await update.message.reply_text(
                f"✅ Đã thêm @{target} làm admin với quyền {perm_type}!"
            )
        else:
            await update.message.reply_text("❌ Lỗi khi thêm admin!")

    @auto_update_user
    @require_group_permission('manage')
    async def add_admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Thêm admin cho group (chỉ owner mới dùng được)"""
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not ctx.args:
            await update.message.reply_text(
                "📝 *HƯỚNG DẪN THÊM ADMIN*\n\n"
                "• `/addadmin @user view` - Thêm quyền xem\n"
                "• `/addadmin @user edit` - Thêm quyền sửa\n"
                "• `/addadmin @user delete` - Thêm quyền xóa\n"
                "• `/addadmin @user manage` - Thêm quyền quản lý\n"
                "• `/addadmin @user full` - Thêm toàn quyền\n\n"
                "Ví dụ: `/addadmin @john view`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        target = ctx.args[0]
        perm_type = ctx.args[1] if len(ctx.args) > 1 else 'view'
        
        # Tìm user ID
        if target.startswith('@'):
            username = target[1:]
            admin_id = get_user_id_by_username(username)
            if not admin_id:
                await update.message.reply_text(f"❌ Không tìm thấy user {target}")
                return
        else:
            try:
                admin_id = int(target)
            except:
                await update.message.reply_text("❌ ID không hợp lệ!")
                return
        
        # Xác định quyền
        permissions = {'view': 0, 'edit': 0, 'delete': 0, 'manage': 0}
        
        if perm_type == 'view':
            permissions['view'] = 1
        elif perm_type == 'edit':
            permissions['view'] = 1
            permissions['edit'] = 1
        elif perm_type == 'delete':
            permissions['view'] = 1
            permissions['delete'] = 1
        elif perm_type == 'manage':
            permissions['manage'] = 1
        elif perm_type == 'full':
            permissions['view'] = 1
            permissions['edit'] = 1
            permissions['delete'] = 1
            permissions['manage'] = 1
        else:
            await update.message.reply_text("❌ Loại quyền không hợp lệ!")
            return
        
        if grant_admin_permission(chat_id, admin_id, update.effective_user.id, permissions):
            await update.message.reply_text(
                f"✅ Đã thêm {target} làm admin với quyền {perm_type}!"
            )
        else:
            await update.message.reply_text("❌ Lỗi khi thêm admin!")

    @auto_update_user
    @require_group_permission('view')
    async def list_admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xem danh sách admin trong group"""
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Lấy danh sách admin từ database
        admins = get_all_admins(chat_id)
        
        if not admins:
            await update.message.reply_text("📭 Chưa có admin nào trong group!")
            return
        
        msg = "👑 *DANH SÁCH ADMIN*\n━━━━━━━━━━━━━━━━\n\n"
        for admin in admins:
            # Kiểm tra cấu trúc dữ liệu trả về từ get_all_admins
            if len(admin) >= 7:
                admin_id, view, edit, delete, manage, username, first_name = admin
            else:
                # Nếu là cấu trúc cũ
                admin_id, view, edit, delete, manage = admin[:5]
                username = None
                first_name = None
            
            # Tạo tên hiển thị
            if username:
                display = f"@{username}"
            elif first_name:
                display = first_name
            else:
                display = f"User {admin_id}"
            
            # Liệt kê quyền
            permissions = []
            if view: permissions.append("👁 Xem")
            if edit: permissions.append("✏️ Sửa")
            if delete: permissions.append("🗑 Xóa")
            if manage: permissions.append("🔐 Quản lý")
            
            msg += f"• {display} (`{admin_id}`)\n"
            msg += f"  Quyền: {', '.join(permissions)}\n"
            msg += f"  Ngày thêm: {admin[7][:10] if len(admin) > 7 else 'N/A'}\n\n"
        
        msg += f"🕐 {format_vn_time()}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
    @require_group_permission('manage')
    async def remove_admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xóa admin khỏi group"""
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not ctx.args:
            await update.message.reply_text(
                "📝 *HƯỚNG DẪN XÓA ADMIN*\n\n"
                "• `/removeadmin @user` - Xóa admin\n"
                "• `/removeadmin ID` - Xóa admin bằng ID\n\n"
                "Ví dụ: `/removeadmin @john`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        target = ctx.args[0]
        
        # Tìm user ID
        if target.startswith('@'):
            username = target[1:]
            admin_id = get_user_id_by_username(username)
            if not admin_id:
                await update.message.reply_text(f"❌ Không tìm thấy user {target}")
                return
        else:
            try:
                admin_id = int(target)
            except:
                await update.message.reply_text("❌ ID không hợp lệ!")
                return
        
        # Không cho xóa chính mình
        if admin_id == update.effective_user.id:
            await update.message.reply_text("❌ Không thể tự xóa quyền admin của chính mình!")
            return
        
        # Kiểm tra xem có phải owner không
        if admin_id == get_group_owner(chat_id):
            await update.message.reply_text("❌ Không thể xóa chủ sở hữu group!")
            return
        
        if revoke_admin_permission(chat_id, admin_id):
            await update.message.reply_text(f"✅ Đã xóa {target} khỏi danh sách admin!")
        else:
            await update.message.reply_text(f"❌ Không tìm thấy {target} trong danh sách admin!")

    @auto_update_user
    @require_group_permission('delete')
    async def delete_category_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xóa danh mục chi tiêu"""
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        if not ctx.args:
            # Hiển thị danh sách danh mục để chọn
            categories = get_expense_categories(owner_id)
            
            if not categories:
                await update.message.reply_text("📭 Chưa có danh mục nào để xóa!")
                return
            
            msg = "🗑 *CHỌN DANH MỤC CẦN XÓA*\n━━━━━━━━━━━━━━━━\n\n"
            keyboard = []
            row = []
            
            for i, cat in enumerate(categories, 1):
                cat_id, name, budget, created = cat
                msg += f"{i}. *{name}* - {format_currency_simple(budget, 'VND')}\n"
                
                row.append(InlineKeyboardButton(f"{i}", callback_data=f"del_cat_{cat_id}"))
                if len(row) == 5:
                    keyboard.append(row)
                    row = []
            
            if row:
                keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")])
            
            msg += f"\n🕐 {format_vn_time_short()}"
            
            await update.message.reply_text(
                msg,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        # Nếu có argument, xóa trực tiếp theo ID
        try:
            category_id = int(ctx.args[0])
            
            # Hỏi xác nhận
            keyboard = [[
                InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"confirm_del_cat_{category_id}"),
                InlineKeyboardButton("❌ Hủy", callback_data="expense_categories")
            ]]
            
            # Lấy thông tin danh mục
            categories = get_expense_categories(owner_id)
            category_name = "Không xác định"
            for cat in categories:
                if cat[0] == category_id:
                    category_name = cat[1]
                    break
            
            await update.message.reply_text(
                f"⚠️ *CẢNH BÁO: XÓA DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                f"📋 Danh mục: *{category_name}* (ID: {category_id})\n\n"
                f"❗️ Hành động này sẽ xóa:\n"
                f"• Danh mục *{category_name}*\n"
                f"• Tất cả chi tiêu trong danh mục này\n\n"
                f"❌ *Không thể khôi phục!*\n\n"
                f"Bạn có chắc chắn muốn xóa?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except ValueError:
            await update.message.reply_text("❌ ID không hợp lệ!")

    @auto_update_user
    @require_group_permission('delete')
    async def quick_delete_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xóa danh mục nhanh bằng cách reply"""
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ Hãy reply tin nhắn chứa ID danh mục cần xóa!")
            return
        
        # Lấy ID từ tin nhắn được reply
        reply_text = update.message.reply_to_message.text
        import re
        match = re.search(r'\*(\d+)\.\*', reply_text) or re.search(r'ID: (\d+)', reply_text)
        
        if not match:
            await update.message.reply_text("❌ Không tìm thấy ID danh mục trong tin nhắn được reply!")
            return
        
        category_id = int(match.group(1))
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        # Hỏi xác nhận
        keyboard = [[
            InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"confirm_del_cat_{category_id}"),
            InlineKeyboardButton("❌ Hủy", callback_data="expense_categories")
        ]]
        
        await update.message.reply_text(
            f"⚠️ *XÁC NHẬN XÓA DANH MỤC #{category_id}*\n\n"
            f"Bạn có chắc chắn muốn xóa?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    @auto_update_user
    async def balance_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xem cân đối thu chi"""
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type in ['group', 'supergroup']:
            current_user = update.effective_user.id
            if current_user != owner_id and not check_permission(chat_id, current_user, 'view'):
                await update.message.reply_text("❌ Bạn không có quyền xem dữ liệu!")
                return
        
        period = 'month'
        if ctx.args:
            arg = ctx.args[0].lower()
            if arg in ['day', 'ngay', 'hôm nay', 'today', 'd']:
                period = 'day'
            elif arg in ['month', 'thang', 'tháng', 'this month', 'm']:
                period = 'month'
            elif arg in ['year', 'nam', 'năm', 'this year', 'y']:
                period = 'year'
            elif arg in ['all', 'tat ca', 'tất cả', 'all time', 'a']:
                period = 'all'
        
        msg = await update.message.reply_text("🔄 Đang tính toán cân đối...")
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (owner_id,))
        user_info = c.fetchone()
        conn.close()
        
        user_name = f"@{user_info[0]}" if user_info and user_info[0] else (user_info[1] if user_info else "")
        
        balance_data = get_balance_summary(owner_id, period)
        
        if not balance_data:
            await msg.edit_text("❌ Không thể tính cân đối!")
            return
        
        balance_msg = format_balance_message(balance_data, user_name)
        
        keyboard = [
            [InlineKeyboardButton("📅 Hôm nay", callback_data="balance_day"),
             InlineKeyboardButton("📅 Tháng này", callback_data="balance_month")],
            [InlineKeyboardButton("📅 Năm nay", callback_data="balance_year"),
             InlineKeyboardButton("📊 Tất cả", callback_data="balance_all")],
            [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]
        ]
        
        await msg.edit_text(
            balance_msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    @auto_update_user
    async def show_portfolio_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xử lý callback khi xem portfolio"""
        query = update.callback_query
        await query.answer()
        
        current_user = query.from_user.id
        chat_id = query.message.chat.id
        chat_type = query.message.chat.type
        
        # Trong group, luôn xem portfolio của owner
        if chat_type in ['group', 'supergroup']:
            owner_id = get_group_owner(chat_id)
            
            # Kiểm tra quyền xem
            if current_user != owner_id and not check_permission(chat_id, current_user, 'view'):
                await query.edit_message_text(
                    "❌ Bạn không có quyền xem portfolio!",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                )
                return
            
            target_user_id = owner_id
            target_name = "của group"
        else:
            # Private chat - xem của chính mình
            target_user_id = current_user
            target_name = "của bạn"
        
        # Lấy portfolio
        portfolio_data = get_portfolio(target_user_id)
        
        if not portfolio_data:
            await query.edit_message_text(
                f"📭 Danh mục {target_name} trống!\n\n🕐 {format_vn_time()}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
            )
            return
        
        # Hiển thị portfolio (giữ nguyên logic hiển thị cũ)
        # ... code hiển thị portfolio ...

    # ==================== PERMISSION COMMAND ====================
    async def perm_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # KIỂM TRA VÀ AUTO-GRANT CHO USER ĐẦU TIÊN
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Đếm số lượng admin đã được cấp quyền trong group này
        c.execute("SELECT COUNT(*) FROM permissions WHERE group_id = ?", (chat_id,))
        admin_count = c.fetchone()[0]
        
        # Nếu CHƯA CÓ AI ĐƯỢC CẤP QUYỀN, auto grant cho user hiện tại
        if admin_count == 0:
            permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 1}
            if grant_permission(chat_id, user_id, user_id, permissions):
                await update.message.reply_text(
                    "👑 *BẠN LÀ ADMIN ĐẦU TIÊN*\n\n"
                    "✅ Đã tự động cấp toàn quyền!\n"
                    "Dùng `/perm list` để xem danh sách.",
                    parse_mode=ParseMode.MARKDOWN
                )
                # QUAN TRỌNG: Update user info ngay lập tức
                await update_user_info_async(update.effective_user)
                conn.close()
                return
        
        conn.close()
        
        # Tiếp tục logic kiểm tra quyền bình thường
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền quản lý phân quyền!")
            return
        
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền quản lý phân quyền!")
            return
        
        if not ctx.args:
            msg = (
                "🔐 *QUẢN LÝ PHÂN QUYỀN*\n━━━━━━━━━━━━━━━━\n\n"
                "*Các lệnh:*\n"
                "• `/perm list` - Xem danh sách admin\n"
                "• `/perm grant @user view` - Cấp quyền xem\n"
                "• `/perm grant @user edit` - Cấp quyền sửa\n"
                "• `/perm grant @user delete` - Cấp quyền xóa\n"
                "• `/perm grant @user manage` - Cấp quyền quản lý\n"
                "• `/perm grant @user full` - Cấp toàn quyền\n"
                "• `/perm revoke @user` - Thu hồi quyền\n\n"
                f"🕐 {format_vn_time_short()}"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        if ctx.args[0] == "list":
            admins = get_all_admins(chat_id)
            if not admins:
                await update.message.reply_text("📭 Chưa có admin nào được cấp quyền!")
                return
            
            msg = "👑 *DANH SÁCH ADMIN*\n━━━━━━━━━━━━━━━━\n\n"
            for admin in admins:
                # Kiểm tra độ dài của admin tuple
                if len(admin) >= 7:
                    user_id, view, edit, delete, manage, username, first_name = admin
                else:
                    # Nếu là cấu trúc cũ (chỉ 5 cột)
                    user_id, view, edit, delete, manage = admin[:5]
                    username = None
                    first_name = None
                
                # Tạo tên hiển thị
                if username:
                    display = f"`{user_id}` @{username}"
                elif first_name:
                    display = f"`{user_id}` {first_name}"
                else:
                    display = f"`{user_id}`"
                
                permissions = []
                if view: permissions.append("👁 Xem")
                if edit: permissions.append("✏️ Sửa")
                if delete: permissions.append("🗑 Xóa")
                if manage: permissions.append("🔐 Quản lý")
                
                msg += f"• {display}: {', '.join(permissions)}\n"
            
            msg += f"\n🕐 {format_vn_time_short()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        
        elif ctx.args[0] == "grant" and len(ctx.args) >= 3:
            target = ctx.args[1]
            perm_type = ctx.args[2].lower()
            
            target_id = None
            
            # Xử lý username
            if target.startswith('@'):
                username = target[1:]
                target_id = get_user_id_by_username(username)
                
                if not target_id:
                    # Thử tìm trong chat hiện tại
                    try:
                        chat = await ctx.bot.get_chat(username)
                        if chat:
                            target_id = chat.id
                            # Cập nhật vào database ngay lập tức
                            await update_user_info_async(chat)
                    except Exception as e:
                        logger.error(f"Lỗi get_chat: {e}")
                    
                    if not target_id:
                        await update.message.reply_text(
                            f"❌ Không tìm thấy user {target}\n\n"
                            f"💡 *Cách khắc phục:*\n"
                            f"1. Yêu cầu user @{username} nhắn tin cho bot\n"
                            f"2. Hoặc dùng ID trực tiếp: `/perm grant [ID] {perm_type}`\n"
                            f"3. Dùng `/whoami` để xem ID của bạn\n"
                            f"4. Hoặc reply tin nhắn của họ và dùng: `/permgrant {perm_type}`",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return
            else:
                try:
                    target_id = int(target)
                    # Kiểm tra xem user đã tồn tại trong database chưa
                    if not get_user_id_by_username(str(target_id)):
                        # Nếu chưa, thử lấy từ Telegram
                        try:
                            chat = await ctx.bot.get_chat(target_id)
                            if chat:
                                await update_user_info_async(chat)
                        except:
                            pass
                except:
                    await update.message.reply_text("❌ ID không hợp lệ!")
                    return
            
            permissions = {'view': 0, 'edit': 0, 'delete': 0, 'manage': 0}
            
            if perm_type == 'view':
                permissions['view'] = 1
            elif perm_type == 'edit':
                permissions['view'] = 1
                permissions['edit'] = 1
            elif perm_type == 'delete':
                permissions['view'] = 1
                permissions['delete'] = 1
            elif perm_type == 'manage':
                permissions['manage'] = 1
            elif perm_type == 'full':
                permissions['view'] = 1
                permissions['edit'] = 1
                permissions['delete'] = 1
                permissions['manage'] = 1
            else:
                await update.message.reply_text("❌ Loại quyền không hợp lệ!")
                return
            
            if grant_permission(chat_id, target_id, user_id, permissions):
                await update.message.reply_text(f"✅ Đã cấp quyền {perm_type} cho {target}")
            else:
                await update.message.reply_text("❌ Lỗi khi cấp quyền!")
        
        elif ctx.args[0] == "revoke" and len(ctx.args) >= 2:
            target = ctx.args[1]
            
            if target.startswith('@'):
                username = target[1:]
                target_id = get_user_id_by_username(username)
                if not target_id:
                    await update.message.reply_text(f"❌ Không tìm thấy user {target}")
                    return
            else:
                try:
                    target_id = int(target)
                except:
                    await update.message.reply_text("❌ ID không hợp lệ!")
                    return
            
            if revoke_permission(chat_id, target_id):
                await update.message.reply_text(f"✅ Đã thu hồi quyền của {target}")
            else:
                await update.message.reply_text("❌ Không tìm thấy quyền!")

    # ==================== EXPENSE SHORTCUT HANDLERS ====================
    async def expense_shortcut_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        text = update.message.text.strip()
        
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
                
                if add_income(owner_id, amount, source, currency, note):
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
                
                categories = get_expense_categories(owner_id)
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
                
                if add_expense(owner_id, category_id, amount, currency, note):
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
        
        elif text == 'bc':
            incomes_data = get_income_by_period(user_id, 'month')
            expenses_data = get_expenses_by_period(user_id, 'month')
            
            msg = f"📊 *BÁO CÁO THÁNG {get_vn_time().strftime('%m/%Y')}*\n━━━━━━━━━━━━━━━━\n\n"
            
            if incomes_data['transactions']:
                msg += "*💰 THU NHẬP:*\n"
                for inc in incomes_data['transactions'][:5]:
                    id, amount, source, note, currency, date = inc
                    msg += f"• #{id} {date}: {format_currency_simple(amount, currency)} - {source}\n"
                    if note:
                        msg += f"  📝 {note}\n"
                
                msg += f"\n📊 *Tổng thu theo loại tiền:*\n"
                for currency, total in incomes_data['summary'].items():
                    msg += f"  {format_currency_simple(total, currency)}\n"
                msg += f"  *Tổng số:* {incomes_data['total_count']} giao dịch\n\n"
            else:
                msg += "📭 Chưa có thu nhập trong tháng này.\n\n"
            
            if expenses_data['transactions']:
                msg += "*💸 CHI TIÊU:*\n"
                for exp in expenses_data['transactions'][:5]:
                    id, cat_name, amount, note, currency, date, budget = exp
                    msg += f"• #{id} {date}: {format_currency_simple(amount, currency)} - {cat_name}\n"
                    if note:
                        msg += f"  📝 {note}\n"
                
                msg += f"\n📊 *Tổng chi theo loại tiền:*\n"
                for currency, total in expenses_data['summary'].items():
                    msg += f"  {format_currency_simple(total, currency)}\n"
                
                msg += f"\n📋 *Chi tiêu theo danh mục:*\n"
                for key, data in expenses_data['category_summary'].items():
                    budget_status = ""
                    if data['budget'] > 0:
                        percent = (data['total'] / data['budget']) * 100
                        if percent > 100:
                            budget_status = " ⚠️ Vượt budget!"
                        elif percent > 80:
                            budget_status = " ⚠️ Gần hết budget"
                        msg += f"  • {data['category']} ({data['currency']}): {format_currency_simple(data['total'], data['currency'])} ({data['count']} lần) - Budget: {format_currency_simple(data['budget'], 'VND')}{budget_status}\n"
                    else:
                        msg += f"  • {data['category']} ({data['currency']}): {format_currency_simple(data['total'], data['currency'])} ({data['count']} lần)\n"
                
                msg += f"\n  *Tổng số:* {expenses_data['total_count']} giao dịch\n"
            else:
                msg += "📭 Không có chi tiêu trong tháng này."
            
            msg += f"\n\n*⚖️ CÂN ĐỐI THEO LOẠI TIỀN:*\n"
            all_currencies = set(list(incomes_data['summary'].keys()) + list(expenses_data['summary'].keys()))
            
            for currency in all_currencies:
                income = incomes_data['summary'].get(currency, 0)
                expense = expenses_data['summary'].get(currency, 0)
                balance = income - expense
                if balance > 0:
                    emoji = "✅"
                elif balance < 0:
                    emoji = "❌"
                else:
                    emoji = "➖"
                
                msg += f"  {emoji} {currency}: Thu {format_currency_simple(income, currency)} - Chi {format_currency_simple(expense, currency)} = {format_currency_simple(balance, currency)}\n"
            
            msg += f"\n🕐 {format_vn_time()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        
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

    # ==================== HANDLE MESSAGE ====================
    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user:
            await update_user_info_async(update.effective_user)
        
        logger.info(f"Nhận tin nhắn từ user {update.effective_user.id} trong chat {update.effective_chat.type}: {update.message.text}")
        
        text = update.message.text.strip()
        chat_type = update.effective_chat.type
        
        # Tính toán đơn giản
        if re.search(r'[\+\-\*\/]', text) and re.match(r'^[\d\s\+\-\*\/\.\(\)]+$', text):
            try:
                result = eval(text, {"__builtins__": {}}, {})
                if isinstance(result, float):
                    if result.is_integer():
                        result = int(result)
                    else:
                        result = round(result, 6)
                await update.message.reply_text(f"`{result}`", parse_mode=ParseMode.MARKDOWN)
                return
            except:
                return
        
        # XỬ LÝ CÁC LỆNH CHI TIÊU - CHO PHÉP CẢ PRIVATE VÀ GROUP
        if text.startswith(('tn ', 'dm ', 'ct ', 'ds', 'bc', 'xoa chi ', 'xoa thu ')):
            # Nếu là group, kiểm tra quyền
            if chat_type in ['group', 'supergroup']:
                user_id = update.effective_user.id
                chat_id = update.effective_chat.id
                effective_user_id = ctx.bot_data.get('effective_user_id', user_id)
                
                # Nếu user đang thao tác trên data của chủ sở hữu (khác với user_id của họ)
                if user_id != effective_user_id:
                    # Kiểm tra quyền xem
                    if not check_permission(chat_id, user_id, 'view'):
                        await update.message.reply_text(
                            "❌ Bạn không có quyền thêm dữ liệu trong group này!\n"
                            "Vui lòng liên hệ chủ sở hữu để được cấp quyền.",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return
            
            # Nếu đã qua kiểm tra quyền, xử lý lệnh
            await expense_shortcut_handler(update, ctx)
            return
        
        # XỬ LÝ MENU
        if text == "💰 ĐẦU TƯ COIN":
            await update.message.reply_text(
                f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_invest_menu_keyboard(update.effective_user.id, update.effective_chat.id)
            )
        elif text == "💸 QUẢN LÝ CHI TIÊU":
            await update.message.reply_text(
                f"💰 *QUẢN LÝ CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_expense_menu_keyboard()
            )
        elif text == "❓ HƯỚNG DẪN":
            await help_command(update, ctx)

    # ==================== EXPORT CSV HANDLER ====================
    async def export_csv_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Xử lý xuất CSV cho cả portfolio và expense"""
        query = update.callback_query
        user_id = ctx.bot_data.get('effective_user_id', query.from_user.id)
        
        await query.edit_message_text("🔄 Đang tạo file CSV...")
        
        try:
            if query.data == "export_csv":
                # Xuất portfolio
                transactions = get_transaction_detail(user_id)
                if not transactions:
                    await query.edit_message_text(
                        "📭 Không có dữ liệu portfolio để xuất!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                # Tạo file CSV
                timestamp = get_vn_time().strftime('%Y%m%d_%H%M%S')
                filename = f"portfolio_{user_id}_{timestamp}.csv"
                filepath = os.path.join(EXPORT_DIR, filename)
                
                logger.info(f"📝 Đang tạo file CSV: {filepath}")
                
                # Ghi file
                with open(filepath, 'w', newline='', encoding='utf-8-sig') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(['ID', 'Mã coin', 'Số lượng', 'Giá mua (USD)', 'Ngày mua', 'Tổng vốn (USD)'])
                    for tx in transactions:
                        writer.writerow([tx[0], tx[1], tx[2], tx[3], tx[4], tx[5]])
                
                # Kiểm tra file đã được tạo chưa
                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    logger.info(f"✅ File đã tạo: {filepath}, kích thước: {file_size} bytes")
                    
                    # Gửi file
                    with open(filepath, 'rb') as f:
                        await query.message.reply_document(
                            document=f,
                            filename=filename,
                            caption=f"📊 *BÁO CÁO DANH MỤC ĐẦU TƯ*\n━━━━━━━━━━━━━━━━\n\n✅ Xuất thành công {len(transactions)} giao dịch!\n📁 File: `{filename}`\n🕐 {format_vn_time()}",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    
                    # Xóa file tạm
                    os.remove(filepath)
                    logger.info(f"🗑 Đã xóa file tạm: {filepath}")
                else:
                    logger.error(f"❌ Không tìm thấy file sau khi tạo: {filepath}")
                    await query.edit_message_text("❌ Lỗi: Không thể tạo file CSV!")
                    return
                
                # Quay lại menu
                await query.edit_message_text(
                    f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_invest_menu_keyboard(user_id, query.message.chat.id)
                )
                
            elif query.data == "expense_export":
                # Xuất báo cáo chi tiêu
                expenses = get_recent_expenses(user_id, 1000)  # Lấy nhiều hơn
                incomes = get_recent_incomes(user_id, 1000)
                
                if not expenses and not incomes:
                    await query.edit_message_text(
                        "📭 Không có dữ liệu chi tiêu để xuất!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                    )
                    return
                
                timestamp = get_vn_time().strftime('%Y%m%d_%H%M%S')
                filename = f"expense_report_{user_id}_{timestamp}.csv"
                filepath = os.path.join(EXPORT_DIR, filename)
                
                logger.info(f"📝 Đang tạo file CSV: {filepath}")
                
                with open(filepath, 'w', newline='', encoding='utf-8-sig') as csvfile:
                    writer = csv.writer(csvfile)
                    
                    # Thu nhập
                    writer.writerow(['=== THU NHẬP ==='])
                    writer.writerow(['ID', 'Ngày', 'Nguồn', 'Số tiền', 'Loại tiền', 'Ghi chú'])
                    for inc in incomes:
                        writer.writerow([inc[0], inc[4], inc[2], inc[1], inc[5], inc[3]])
                    
                    writer.writerow([])  # Dòng trống
                    
                    # Chi tiêu
                    writer.writerow(['=== CHI TIÊU ==='])
                    writer.writerow(['ID', 'Ngày', 'Danh mục', 'Số tiền', 'Loại tiền', 'Ghi chú'])
                    for exp in expenses:
                        writer.writerow([exp[0], exp[4], exp[1], exp[2], exp[5], exp[3]])
                
                # Kiểm tra file đã được tạo chưa
                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    logger.info(f"✅ File đã tạo: {filepath}, kích thước: {file_size} bytes")
                    
                    # Gửi file
                    with open(filepath, 'rb') as f:
                        await query.message.reply_document(
                            document=f,
                            filename=filename,
                            caption=f"📊 *BÁO CÁO THU CHI*\n━━━━━━━━━━━━━━━━\n\n✅ Xuất thành công!\n• Thu nhập: {len(incomes)} giao dịch\n• Chi tiêu: {len(expenses)} giao dịch\n📁 File: `{filename}`\n\n🕐 {format_vn_time()}",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    
                    os.remove(filepath)
                    logger.info(f"🗑 Đã xóa file tạm: {filepath}")
                else:
                    logger.error(f"❌ Không tìm thấy file sau khi tạo: {filepath}")
                    await query.edit_message_text("❌ Lỗi: Không thể tạo file CSV!")
                    return
                
                # Quay lại menu
                await query.edit_message_text(
                    f"💰 *QUẢN LÝ CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_expense_menu_keyboard()
                )
                
        except Exception as e:
            logger.error(f"❌ Lỗi export CSV: {e}", exc_info=True)
            await query.edit_message_text(
                f"❌ Lỗi khi xuất CSV: {str(e)[:200]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_main")]])
            )
            
            # Dọn dẹp file nếu có lỗi
            try:
                if 'filepath' in locals() and os.path.exists(filepath):
                    os.remove(filepath)
                    logger.info(f"🗑 Đã dọn dẹp file lỗi: {filepath}")
            except:
                pass
            
    # ==================== CALLBACK HANDLER ====================
    async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.from_user:
            await update_user_info_async(query.from_user)
        logger.info(f"Callback: {query.data}")
        
        data = query.data
        
        try:
            if data == "back_to_main":
                await query.edit_message_text(
                    f"💰 *MENU CHÍNH*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=None
                )
                await query.message.reply_text("👇 Chọn chức năng:", reply_markup=get_main_keyboard())
            
            elif data == "back_to_invest":
                uid = query.from_user.id
                gid = query.message.chat.id
                await query.edit_message_text(
                    f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_invest_menu_keyboard(uid, gid)
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
            
            elif data.startswith("price_"):
                symbol = data.replace("price_", "")
                d = get_price(symbol)
                
                if d:
                    if symbol == 'USDT':
                        rate_data = get_usdt_vnd_rate()
                        msg = f"*{d['n']}* #{d['r']}\n💰 USD: `{fmt_price(d['p'])}`\n🇻🇳 VND: `{fmt_vnd(rate_data['vnd'])}`\n📦 Volume: `{fmt_vol(d['v'])}`\n💎 Market Cap: `{fmt_vol(d['m'])}`\n📈 24h: {fmt_percent(d['c'])}"
                    else:
                        msg = f"*{d['n']}* #{d['r']}\n💰 Giá: `{fmt_price(d['p'])}`\n📦 Volume: `{fmt_vol(d['v'])}`\n💎 Market Cap: `{fmt_vol(d['m'])}`\n📈 24h: {fmt_percent(d['c'])}"
                    msg += f"\n\n🕐 {format_vn_time_short()}"
                else:
                    msg = f"❌ *{symbol}*: Không có dữ liệu\n\n🕐 {format_vn_time_short()}"
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            
            elif data == "show_portfolio":
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                
                # KIỂM TRA XEM CÓ ĐANG XEM CỦA AI KHÔNG
                # Lưu ý: Cần có cơ chế để chọn user cần xem
                # Tạm thời, chúng ta sẽ thêm nút chọn user trong group
                
                # Lấy danh sách user đã tương tác trong group
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT DISTINCT user_id, username, first_name 
                             FROM users 
                             WHERE user_id IN (SELECT DISTINCT user_id FROM portfolio)
                             ORDER BY last_seen DESC
                             LIMIT 10''')
                users_with_portfolio = c.fetchall()
                conn.close()
                
                if not users_with_portfolio:
                    await query.edit_message_text(
                        f"📭 Chưa có ai có danh mục đầu tư!\n\n🕐 {format_vn_time()}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                # Tạo menu chọn user
                msg = "👥 *CHỌN USER XEM DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                keyboard = []
                row = []
                
                for i, (uid, username, first_name) in enumerate(users_with_portfolio, 1):
                    display = f"@{username}" if username else first_name or f"User {uid}"
                    display_short = display[:15] + "..." if len(display) > 15 else display
                    msg += f"{i}. {display}\n"
                    
                    row.append(InlineKeyboardButton(f"{i}", callback_data=f"view_portfolio_{uid}"))
                    if len(row) == 5:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                
                keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")])
                
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await query.edit_message_text(
                    msg, 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data.startswith("view_portfolio_"):
                target_user_id = int(data.replace("view_portfolio_", ""))
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                
                # Kiểm tra quyền
                if current_user_id != target_user_id:
                    if not check_permission(chat_id, current_user_id, 'view'):
                        await query.edit_message_text(
                            "❌ Bạn không có quyền xem dữ liệu của người khác!",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                        )
                        return
                
                # Lấy thông tin user target
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
                user_info = c.fetchone()
                conn.close()
                
                display_name = user_info[0] if user_info and user_info[0] else (user_info[1] if user_info else f"User {target_user_id}")
                
                # Lấy portfolio của target user
                portfolio_data = get_portfolio(target_user_id)
                
                if not portfolio_data:
                    await query.edit_message_text(
                        f"📭 Danh mục của {display_name} trống!\n\n🕐 {format_vn_time()}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                # Lấy giá batch
                symbols = list(set([row[0] for row in portfolio_data]))
                prices = get_prices_batch(symbols)
                
                # Tính toán portfolio
                summary = {}
                total_invest = 0
                total_value = 0
                
                for row in portfolio_data:
                    symbol, amount, price, date, cost = row
                    if symbol not in summary:
                        summary[symbol] = {'amount': 0, 'cost': 0}
                    summary[symbol]['amount'] += amount
                    summary[symbol]['cost'] += cost
                    total_invest += cost
                
                msg = f"📊 *DANH MỤC CỦA {display_name}*\n━━━━━━━━━━━━━━━━\n\n"
                
                for symbol, data in summary.items():
                    price_data = prices.get(symbol)
                    if price_data:
                        current = data['amount'] * price_data['p']
                        profit = current - data['cost']
                        profit_percent = (profit / data['cost']) * 100 if data['cost'] > 0 else 0
                        total_value += current
                        
                        msg += f"*{symbol}*\n"
                        msg += f"📊 SL: `{data['amount']:.4f}`\n"
                        msg += f"💰 TB: `{fmt_price(data['cost']/data['amount'])}`\n"
                        msg += f"💎 TT: `{fmt_price(current)}`\n"
                        msg += f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n"
                
                total_profit = total_value - total_invest
                total_profit_percent = (total_profit / total_invest) * 100 if total_invest > 0 else 0
                
                msg += "━━━━━━━━━━━━━━━━\n"
                msg += f"💵 Vốn: `{fmt_price(total_invest)}`\n"
                msg += f"💰 GT: `{fmt_price(total_value)}`\n"
                msg += f"{'✅' if total_profit>=0 else '❌'} Tổng LN: `{fmt_price(total_profit)}` ({total_profit_percent:+.2f}%)\n\n"
                msg += f"🕐 {format_vn_time()}"
                
                keyboard = [[
                    InlineKeyboardButton("👥 Xem user khác", callback_data="show_portfolio"),
                    InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")
                ]]
                
                await query.edit_message_text(
                    msg, 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data == "show_profit":
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                
                # Hiển thị danh sách user để chọn
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT DISTINCT user_id, username, first_name 
                             FROM users 
                             WHERE user_id IN (SELECT DISTINCT user_id FROM portfolio)
                             ORDER BY last_seen DESC
                             LIMIT 10''')
                users_with_portfolio = c.fetchall()
                conn.close()
                
                if not users_with_portfolio:
                    await query.edit_message_text(
                        f"📭 Chưa có ai có danh mục đầu tư!\n\n🕐 {format_vn_time()}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                msg = "📈 *CHỌN USER XEM LỢI NHUẬN*\n━━━━━━━━━━━━━━━━\n\n"
                keyboard = []
                row = []
                
                for i, (uid, username, first_name) in enumerate(users_with_portfolio, 1):
                    display = f"@{username}" if username else first_name or f"User {uid}"
                    msg += f"{i}. {display}\n"
                    row.append(InlineKeyboardButton(f"{i}", callback_data=f"view_profit_{uid}"))
                    if len(row) == 5:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                
                keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")])
                
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await query.edit_message_text(
                    msg, 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data.startswith("view_profit_"):
                target_user_id = int(data.replace("view_profit_", ""))
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                
                # Kiểm tra quyền
                if current_user_id != target_user_id:
                    if not check_permission(chat_id, current_user_id, 'view'):
                        await query.edit_message_text(
                            "❌ Bạn không có quyền xem dữ liệu của người khác!",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                        )
                        return
                
                # Lấy thông tin user target
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
                user_info = c.fetchone()
                conn.close()
                
                display_name = user_info[0] if user_info and user_info[0] else (user_info[1] if user_info else f"User {target_user_id}")
                
                # Lấy transactions của target user
                transactions = get_transaction_detail(target_user_id)
                
                if not transactions:
                    await query.edit_message_text(
                        f"📭 Danh mục của {display_name} trống!\n\n🕐 {format_vn_time()}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                msg = f"📈 *CHI TIẾT LỢI NHUẬN - {display_name}*\n━━━━━━━━━━━━━━━━\n\n"
                total_invest = 0
                total_value = 0
                
                for tx in transactions:
                    tx_id, symbol, amount, price, date, cost = tx
                    price_data = get_price(symbol)
                    
                    if price_data:
                        current = amount * price_data['p']
                        profit = current - cost
                        profit_percent = (profit / cost) * 100 if cost > 0 else 0
                        
                        total_invest += cost
                        total_value += current
                        
                        short_date = date.split()[0]
                        msg += f"*#{tx_id}: {symbol}*\n"
                        msg += f"📅 {short_date}\n"
                        msg += f"📊 SL: `{amount:.4f}`\n"
                        msg += f"💰 Mua: `{fmt_price(price)}`\n"
                        msg += f"💎 TT: `{fmt_price(current)}`\n"
                        msg += f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n"
                
                total_profit = total_value - total_invest
                total_profit_percent = (total_profit / total_invest) * 100 if total_invest > 0 else 0
                
                msg += "━━━━━━━━━━━━━━━━\n"
                msg += f"💵 Vốn: `{fmt_price(total_invest)}`\n"
                msg += f"💰 GT: `{fmt_price(total_value)}`\n"
                msg += f"{'✅' if total_profit>=0 else '❌'} Tổng LN: `{fmt_price(total_profit)}` ({total_profit_percent:+.2f}%)\n\n"
                msg += f"🕐 {format_vn_time()}"
                
                keyboard = [[
                    InlineKeyboardButton("👥 Xem user khác", callback_data="show_profit"),
                    InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")
                ]]
                
                await query.edit_message_text(
                    msg, 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data == "show_stats":
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                
                # Hiển thị danh sách user để chọn
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT DISTINCT user_id, username, first_name 
                             FROM users 
                             WHERE user_id IN (SELECT DISTINCT user_id FROM portfolio)
                             ORDER BY last_seen DESC
                             LIMIT 10''')
                users_with_portfolio = c.fetchall()
                conn.close()
                
                if not users_with_portfolio:
                    await query.edit_message_text(
                        f"📭 Chưa có ai có danh mục đầu tư!\n\n🕐 {format_vn_time()}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                msg = "📊 *CHỌN USER XEM THỐNG KÊ*\n━━━━━━━━━━━━━━━━\n\n"
                keyboard = []
                row = []
                
                for i, (uid, username, first_name) in enumerate(users_with_portfolio, 1):
                    display = f"@{username}" if username else first_name or f"User {uid}"
                    msg += f"{i}. {display}\n"
                    row.append(InlineKeyboardButton(f"{i}", callback_data=f"view_stats_{uid}"))
                    if len(row) == 5:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                
                keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")])
                
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await query.edit_message_text(
                    msg, 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data.startswith("view_stats_"):
                target_user_id = int(data.replace("view_stats_", ""))
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                
                # Kiểm tra quyền
                if current_user_id != target_user_id:
                    if not check_permission(chat_id, current_user_id, 'view'):
                        await query.edit_message_text(
                            "❌ Bạn không có quyền xem dữ liệu của người khác!",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                        )
                        return
                
                # Lấy thông tin user target
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
                user_info = c.fetchone()
                conn.close()
                
                display_name = user_info[0] if user_info and user_info[0] else (user_info[1] if user_info else f"User {target_user_id}")
                
                await query.edit_message_text("🔄 Đang tính toán thống kê...")
                
                stats = get_portfolio_stats(target_user_id)
                
                if not stats:
                    await query.edit_message_text(
                        f"📭 Danh mục của {display_name} trống!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                msg = (
                    f"📊 *THỐNG KÊ - {display_name}*\n━━━━━━━━━━━━━━━━\n\n"
                    f"*TỔNG QUAN*\n"
                    f"• Vốn: `{fmt_price(stats['total_invest'])}`\n"
                    f"• Giá trị: `{fmt_price(stats['total_value'])}`\n"
                    f"• Lợi nhuận: `{fmt_price(stats['total_profit'])}`\n"
                    f"• Tỷ suất: `{stats['total_profit_percent']:+.2f}%`\n\n"
                    f"*📈 TOP COIN LỜI NHẤT*\n"
                )
                
                count = 0
                for symbol, profit, profit_pct, value, cost in stats['coin_profits']:
                    if profit > 0:
                        count += 1
                        msg += f"{count}. *{symbol}*: `{fmt_price(profit)}` ({profit_pct:+.2f}%)\n"
                    if count >= 3:
                        break
                
                if count == 0:
                    msg += "Không có coin lời\n"
                
                msg += f"\n*📉 TOP COIN LỖ NHẤT*\n"
                count = 0
                for symbol, profit, profit_pct, value, cost in reversed(stats['coin_profits']):
                    if profit < 0:
                        count += 1
                        msg += f"{count}. *{symbol}*: `{fmt_price(profit)}` ({profit_pct:+.2f}%)\n"
                    if count >= 3:
                        break
                
                if count == 0:
                    msg += "Không có coin lỗ\n"
                
                msg += f"\n🕐 {format_vn_time()}"
                
                keyboard = [[
                    InlineKeyboardButton("👥 Xem user khác", callback_data="show_stats"),
                    InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")
                ]]
                
                await query.edit_message_text(
                    msg, 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data == "show_alerts":
                uid = query.from_user.id
                alerts = get_user_alerts(uid)
                
                if not alerts:
                    await query.edit_message_text(f"📭 Bạn chưa có cảnh báo nào!\n\n🕐 {format_vn_time()}")
                    return
                
                msg = "🔔 *CẢNH BÁO GIÁ*\n━━━━━━━━━━━━━━━━\n\n"
                for alert in alerts:
                    alert_id, symbol, target, condition, created = alert
                    created_date = created.split()[0]
                    price_data = get_price(symbol)
                    current_price = price_data['p'] if price_data else 0
                    status = "🟢" if (condition == 'above' and current_price < target) or (condition == 'below' and current_price > target) else "🔴"
                    msg += f"{status} *#{alert_id}*: {symbol} {condition} `{fmt_price(target)}`\n"
                    msg += f"   Giá hiện: `{fmt_price(current_price)}` (tạo {created_date})\n\n"
                
                msg += f"🕐 {format_vn_time()}"
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

            elif data.startswith("edit_"):
                tx_id = data.replace("edit_", "")
                uid = query.from_user.id
                
                transactions = get_transaction_detail(uid)
                tx = next((t for t in transactions if str(t[0]) == tx_id), None)
                
                if not tx:
                    await query.edit_message_text(f"❌ Không tìm thấy giao dịch #{tx_id}")
                    return
                
                tx_id, symbol, amount, price, date, total = tx
                
                msg = (
                    f"✏️ *SỬA GIAO DỊCH #{tx_id}*\n━━━━━━━━━━━━━━━━\n\n"
                    f"*{symbol}*\n📅 {date}\n"
                    f"📊 SL: `{amount:.4f}`\n"
                    f"💰 Giá: `{fmt_price(price)}`\n\n"
                    f"*Nhập lệnh:*\n`/edit {tx_id} [sl] [giá]`\n\n"
                    f"🕐 {format_vn_time()}"
                )
                
                keyboard = [[
                    InlineKeyboardButton("🗑 Xóa", callback_data=f"del_{tx_id}"),
                    InlineKeyboardButton("🔙 Quay lại", callback_data="edit_transactions")
                ]]
                
                await query.edit_message_text(
                    msg, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data.startswith("del_"):
                tx_id = data.replace("del_", "")
                
                msg = f"⚠️ *Xác nhận xóa giao dịch #{tx_id}?*\n\n🕐 {format_vn_time_short()}"
                keyboard = [[
                    InlineKeyboardButton("✅ Có", callback_data=f"confirm_del_{tx_id}"),
                    InlineKeyboardButton("❌ Không", callback_data="edit_transactions")
                ]]
                
                await query.edit_message_text(
                    msg, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data.startswith("confirm_del_"):
                tx_id = data.replace("confirm_del_", "")
                uid = query.from_user.id
                
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''DELETE FROM portfolio WHERE id = ? AND user_id = ?''', (tx_id, uid))
                conn.commit()
                affected = c.rowcount
                conn.close()
                
                if affected > 0:
                    msg = f"✅ Đã xóa giao dịch #{tx_id}\n\n🕐 {format_vn_time()}"
                else:
                    msg = f"❌ Không thể xóa giao dịch #{tx_id}\n\n🕐 {format_vn_time()}"
                
                keyboard = [[InlineKeyboardButton("🔙 Về danh mục", callback_data="show_portfolio")]]
                
                await query.edit_message_text(
                    msg, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif data.startswith("del_cat_"):
                category_id = int(data.replace("del_cat_", ""))
                owner_id = ctx.bot_data.get('effective_user_id', query.from_user.id)
                
                # Lấy thông tin danh mục
                categories = get_expense_categories(owner_id)
                category_name = "Không xác định"
                for cat in categories:
                    if cat[0] == category_id:
                        category_name = cat[1]
                        break
                
                keyboard = [[
                    InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"confirm_del_cat_{category_id}"),
                    InlineKeyboardButton("❌ Hủy", callback_data="expense_categories")
                ]]
                
                await query.edit_message_text(
                    f"⚠️ *CẢNH BÁO: XÓA DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                    f"📋 Danh mục: *{category_name}* (ID: {category_id})\n\n"
                    f"❗️ Hành động này sẽ xóa:\n"
                    f"• Danh mục *{category_name}*\n"
                    f"• Tất cả chi tiêu trong danh mục này\n\n"
                    f"❌ *Không thể khôi phục!*\n\n"
                    f"Bạn có chắc chắn muốn xóa?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data.startswith("confirm_del_cat_"):
                category_id = int(data.replace("confirm_del_cat_", ""))
                owner_id = ctx.bot_data.get('effective_user_id', query.from_user.id)
                
                await query.edit_message_text("🔄 Đang xóa danh mục...")
                
                success, result, deleted_count = delete_category(category_id, owner_id)
                
                if success:
                    msg = (
                        f"✅ *XÓA DANH MỤC THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n"
                        f"📋 Đã xóa danh mục: *{result}*\n"
                        f"💰 Đã xóa {deleted_count} khoản chi tiêu liên quan\n\n"
                        f"🕐 {format_vn_time()}"
                    )
                else:
                    msg = f"❌ *LỖI*\n━━━━━━━━━━━━━━━━\n\n{result}\n\n🕐 {format_vn_time()}"
                
                keyboard = [[InlineKeyboardButton("📋 Xem danh mục", callback_data="expense_categories")],
                            [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                
                await query.edit_message_text(
                    msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data == "edit_transactions":
                uid = query.from_user.id
                transactions = get_transaction_detail(uid)
                
                if not transactions:
                    await query.edit_message_text(
                        f"📭 Không có giao dịch!\n\n🕐 {format_vn_time()}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                msg = "✏️ *CHỌN GIAO DỊCH*\n━━━━━━━━━━━━\n\n"
                keyboard = []
                row = []
                
                for tx in transactions:
                    tx_id, symbol, amount, price, date, total = tx
                    short_date = date.split()[0]
                    msg += f"• #{tx_id}: {symbol} {amount:.4f} @ {fmt_price(price)} ({short_date})\n"
                    
                    row.append(InlineKeyboardButton(f"#{tx_id}", callback_data=f"edit_{tx_id}"))
                    if len(row) == 4:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                keyboard.append([InlineKeyboardButton("🔙 Về danh mục", callback_data="show_portfolio")])
                
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await query.edit_message_text(
                    msg, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            elif data == "show_top10":
                await query.edit_message_text("🔄 Đang tải...")
                
                try:
                    headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY}
                    res = requests.get(f"{CMC_API_URL}/cryptocurrency/listings/latest", headers=headers, params={'limit': 10, 'convert': 'USD'}, timeout=10)
                    
                    if res.status_code == 200:
                        data = res.json()['data']
                        msg = "📊 *TOP 10 COIN*\n━━━━━━━━━━━━\n\n"
                        
                        for i, coin in enumerate(data, 1):
                            quote = coin['quote']['USD']
                            change = quote['percent_change_24h']
                            emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"
                            
                            msg += f"{i}. *{coin['symbol']}* - {coin['name']}\n"
                            msg += f"   💰 `{fmt_price(quote['price'])}` {emoji} `{change:+.2f}%`\n"
                        
                        msg += f"\n🕐 {format_vn_time_short()}"
                    else:
                        msg = "❌ Không thể lấy dữ liệu"
                except Exception as e:
                    msg = "❌ Lỗi kết nối"
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            
            elif data == "show_buy":
                await query.edit_message_text(
                    "➕ *MUA COIN*\n\n"
                    "Dùng lệnh: `/buy [coin] [sl] [giá]`\n\n"
                    "*Ví dụ:*\n"
                    "• `/buy btc 0.5 40000`\n\n"
                    f"🕐 {format_vn_time_short()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                )
            
            elif data == "show_sell":
                await query.edit_message_text(
                    "➖ *BÁN COIN*\n\n"
                    "Dùng lệnh: `/sell [coin] [sl]`\n\n"
                    "*Ví dụ:*\n"
                    "• `/sell btc 0.2`\n\n"
                    f"🕐 {format_vn_time_short()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                )
            
            elif data == "admin_panel":
                uid = query.from_user.id
                group_id = query.message.chat.id
                
                msg = (
                    "👑 *ADMIN PANEL*\n━━━━━━━━━━━━━━━━\n\n"
                    "• `/perm list` - Danh sách admin\n"
                    "• `/perm grant @user view` - Cấp quyền xem\n"
                    "• `/perm grant @user edit` - Cấp quyền sửa\n"
                    "• `/perm grant @user delete` - Cấp quyền xóa\n"
                    "• `/perm grant @user manage` - Cấp quyền QL\n"
                    "• `/perm revoke @user` - Thu hồi quyền\n\n"
                    "• `/view @user` - Xem portfolio người khác\n"
                    "• `/users` - Xem danh sách thành viên\n"
                    "\n"
                    f"🕐 {format_vn_time()}"
                )
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            
            elif data == "back_to_expense":
                await query.edit_message_text(
                    f"💰 *QUẢN LÝ CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_expense_menu_keyboard()
                )
            
            elif data == "expense_income_menu":
                await query.edit_message_text(
                    "💰 *MENU THU NHẬP*\n\n"
                    "• `tn [số tiền]` - Thêm thu nhập\n"
                    "• `tn 100 USD Lương` - Thêm 100 USD\n\n"
                    f"🕐 {format_vn_time_short()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                )
            
            elif data == "expense_expense_menu":
                await query.edit_message_text(
                    "💸 *MENU CHI TIÊU*\n\n"
                    "• `ct [mã] [số tiền]` - Thêm chi tiêu\n"
                    "• `ct 1 50000 VND Ăn trưa` - Ví dụ\n\n"
                    f"🕐 {format_vn_time_short()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                )
            
            elif data == "expense_categories":
                owner_id = ctx.bot_data.get('effective_user_id', query.from_user.id)
                categories = get_expense_categories(owner_id)
                
                if not categories:
                    await query.edit_message_text(
                        f"📋 Chưa có danh mục nào!\nTạo: `dm [tên] [budget]`\n\n🕐 {format_vn_time_short()}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                    )
                    return
                
                msg = "📋 *DANH MỤC CỦA BẠN*\n━━━━━━━━━━━━━━━━\n\n"
                for cat in categories:
                    cat_id, name, budget, created = cat
                    msg += f"• *{cat_id}.* {name} - {format_currency_simple(budget, 'VND')}\n"
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]))
            
            elif data == "expense_report_menu":
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                effective_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
                
                if current_user_id != effective_user_id and not check_permission(chat_id, current_user_id, 'view'):
                    await query.edit_message_text("❌ Bạn không có quyền xem dữ liệu!")
                    return
                
                expenses = get_expenses_by_period(effective_user_id, 'month')
                incomes = get_income_by_period(effective_user_id, 'month')
                
                msg = f"📊 *BÁO CÁO THÁNG {get_vn_time().strftime('%m/%Y')}*\n━━━━━━━━━━━━━━━━\n\n"
                
                if incomes['transactions']:
                    total_income = 0
                    msg += "*💰 THU NHẬP:*\n"
                    for inc in incomes['transactions'][:5]:
                        id, amount, source, note, currency, date = inc
                        msg += f"• #{id} {date}: {format_currency_simple(amount, currency)} - {source}\n"
                    msg += f"\n"
                else:
                    msg += "📭 Chưa có thu nhập.\n\n"
                
                if expenses['transactions']:
                    total_expense = 0
                    msg += "*💸 CHI TIÊU:*\n"
                    for exp in expenses['transactions'][:5]:
                        id, cat_name, amount, note, currency, date, budget = exp
                        msg += f"• #{id} {date}: {format_currency_simple(amount, currency)} - {cat_name}\n"
                    msg += f"\n"
                else:
                    msg += "📭 Chưa có chi tiêu."
                
                msg += f"\n🕐 {format_vn_time()}"
                
                await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]))
            
            elif data == "expense_today":
                try:
                    current_user_id = query.from_user.id
                    chat_id = query.message.chat.id
                    effective_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
                    
                    if current_user_id != effective_user_id and not check_permission(chat_id, current_user_id, 'view'):
                        await query.edit_message_text("❌ Bạn không có quyền xem dữ liệu!")
                        return
                    
                    incomes_data = get_income_by_period(effective_user_id, 'day')
                    expenses_data = get_expenses_by_period(effective_user_id, 'day')
                    
                    # Lấy thông tin user
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (effective_user_id,))
                    owner_info = c.fetchone()
                    conn.close()
                    
                    # ESCAPE tên owner
                    raw_owner = f"@{owner_info[0]}" if owner_info and owner_info[0] else (owner_info[1] if owner_info else f"User {effective_user_id}")
                    safe_owner = escape_markdown(raw_owner)
                    
                    msg = f"📅 *HÔM NAY ({get_vn_time().strftime('%d/%m/%Y')}) - {safe_owner}*\n━━━━━━━━━━━━━━━━\n\n"
                    
                    if incomes_data['transactions']:
                        msg += "*💰 THU NHẬP:*\n"
                        for inc in incomes_data['transactions']:
                            id, amount, source, note, currency, date = inc
                            # ESCAPE nguồn và ghi chú
                            safe_source = escape_markdown(source)
                            safe_note = escape_markdown(note) if note else ""
                            
                            msg += f"• #{id}: {format_currency_simple(amount, currency)} - {safe_source}\n"
                            if safe_note:
                                msg += f"  📝 {safe_note}\n"
                        
                        msg += f"\n📊 *Tổng thu:*\n"
                        for currency, total in incomes_data['summary'].items():
                            msg += f"  {format_currency_simple(total, currency)}\n"
                        msg += "\n"
                    else:
                        msg += "📭 Không có thu nhập hôm nay.\n\n"
                    
                    if expenses_data['transactions']:
                        msg += "*💸 CHI TIÊU:*\n"
                        for exp in expenses_data['transactions']:
                            id, cat_name, amount, note, currency, date, budget = exp
                            # ESCAPE tên danh mục và ghi chú
                            safe_cat = escape_markdown(cat_name)
                            safe_note = escape_markdown(note) if note else ""
                            
                            msg += f"• #{id}: {format_currency_simple(amount, currency)} - {safe_cat}\n"
                            if safe_note:
                                msg += f"  📝 {safe_note}\n"
                        
                        msg += f"\n📊 *Tổng chi:*\n"
                        for currency, total in expenses_data['summary'].items():
                            msg += f"  {format_currency_simple(total, currency)}\n"
                    else:
                        msg += "📭 Không có chi tiêu hôm nay."
                    
                    msg += f"\n\n🕐 {format_vn_time()}"
                    
                    # KIỂM TRA ĐỘ DÀI TIN NHẮN
                    if len(msg) > 4000:
                        await query.edit_message_text("📊 *Báo cáo quá dài, đang chia nhỏ...*")
                        chunks = [msg[i:i+3500] for i in range(0, len(msg), 3500)]
                        for i, chunk in enumerate(chunks, 1):
                            await query.message.reply_text(
                                f"{chunk}\n\n*(Phần {i}/{len(chunks)})*",
                                parse_mode=ParseMode.MARKDOWN
                            )
                    else:
                        await query.edit_message_text(
                            msg, 
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                        )
                        
                except Exception as e:
                    logger.error(f"Lỗi expense_today: {e}", exc_info=True)
                    await query.edit_message_text(
                        "❌ Có lỗi xảy ra khi xem hôm nay!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                    )
            
            elif data == "expense_month":
                try:
                    current_user_id = query.from_user.id
                    chat_id = query.message.chat.id
                    effective_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
                    
                    if current_user_id != effective_user_id and not check_permission(chat_id, current_user_id, 'view'):
                        await query.edit_message_text("❌ Bạn không có quyền xem dữ liệu!")
                        return
                    
                    incomes_data = get_income_by_period(effective_user_id, 'month')
                    expenses_data = get_expenses_by_period(effective_user_id, 'month')
                    
                    # Lấy thông tin user
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (effective_user_id,))
                    owner_info = c.fetchone()
                    conn.close()
                    
                    # ESCAPE tên owner
                    raw_owner = f"@{owner_info[0]}" if owner_info and owner_info[0] else (owner_info[1] if owner_info else f"User {effective_user_id}")
                    safe_owner = escape_markdown(raw_owner)
                    
                    msg = f"📅 *CHI TIÊU THÁNG {get_vn_time().strftime('%m/%Y')} - {safe_owner}*\n━━━━━━━━━━━━━━━━\n\n"
                    
                    if incomes_data['transactions']:
                        msg += "*💰 THU NHẬP:*\n"
                        for inc in incomes_data['transactions'][:10]:
                            id, amount, source, note, currency, date = inc
                            # ESCAPE nguồn và ghi chú
                            safe_source = escape_markdown(source)
                            safe_note = escape_markdown(note) if note else ""
                            
                            msg += f"• #{id} {date}: {format_currency_simple(amount, currency)} - {safe_source}\n"
                            if safe_note:
                                msg += f"  📝 {safe_note}\n"
                        
                        msg += f"\n📊 *Tổng thu:*\n"
                        for currency, total in incomes_data['summary'].items():
                            msg += f"  {format_currency_simple(total, currency)}\n"
                        msg += f"  *Tổng số:* {incomes_data['total_count']} giao dịch\n\n"
                    else:
                        msg += "📭 Không có thu nhập.\n\n"
                    
                    if expenses_data['transactions']:
                        msg += "*💸 CHI TIÊU:*\n"
                        for exp in expenses_data['transactions'][:10]:
                            id, cat_name, amount, note, currency, date, budget = exp
                            # ESCAPE tên danh mục và ghi chú
                            safe_cat = escape_markdown(cat_name)
                            safe_note = escape_markdown(note) if note else ""
                            
                            msg += f"• #{id} {date}: {format_currency_simple(amount, currency)} - {safe_cat}\n"
                            if safe_note:
                                msg += f"  📝 {safe_note}\n"
                        
                        msg += f"\n📊 *Tổng chi:*\n"
                        for currency, total in expenses_data['summary'].items():
                            msg += f"  {format_currency_simple(total, currency)}\n"
                    else:
                        msg += "📭 Không có chi tiêu."
                    
                    msg += f"\n\n🕐 {format_vn_time()}"
                    
                    # KIỂM TRA ĐỘ DÀI TIN NHẮN
                    if len(msg) > 4000:
                        # Chia nhỏ tin nhắn
                        await query.edit_message_text("📊 *Báo cáo quá dài, đang chia nhỏ...*")
                        
                        chunks = [msg[i:i+3500] for i in range(0, len(msg), 3500)]
                        for i, chunk in enumerate(chunks, 1):
                            await query.message.reply_text(
                                f"{chunk}\n\n*(Phần {i}/{len(chunks)})*",
                                parse_mode=ParseMode.MARKDOWN
                            )
                    else:
                        await query.edit_message_text(
                            msg, 
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                        )
                        
                except Exception as e:
                    logger.error(f"Lỗi expense_month: {e}", exc_info=True)
                    await query.edit_message_text(
                        "❌ Có lỗi xảy ra!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                    )
            
            elif data == "expense_recent":
                try:
                    current_user_id = query.from_user.id
                    chat_id = query.message.chat.id
                    effective_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
                    
                    if current_user_id != effective_user_id and not check_permission(chat_id, current_user_id, 'view'):
                        await query.edit_message_text("❌ Bạn không có quyền xem dữ liệu!")
                        return
                    
                    # Lấy thông tin user
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (effective_user_id,))
                    owner_info = c.fetchone()
                    conn.close()
                    
                    # ESCAPE tên owner
                    raw_owner = f"@{owner_info[0]}" if owner_info and owner_info[0] else (owner_info[1] if owner_info else f"User {effective_user_id}")
                    safe_owner = escape_markdown(raw_owner)
                    
                    recent_incomes = get_recent_incomes(effective_user_id, 20)
                    recent_expenses = get_recent_expenses(effective_user_id, 20)
                    
                    if not recent_incomes and not recent_expenses:
                        await query.edit_message_text(
                            f"📭 *{safe_owner}* chưa có giao dịch nào!\n\n🕐 {format_vn_time_short()}",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                        )
                        return
                    
                    msg = f"🔄 *20 GIAO DỊCH GẦN ĐÂY - {safe_owner}*\n━━━━━━━━━━━━━━━━\n\n"
                    
                    all_transactions = []
                    
                    for inc in recent_incomes:
                        id, amount, source, note, date, currency = inc
                        safe_source = escape_markdown(source)
                        safe_note = escape_markdown(note) if note else ""
                        desc = f"{format_currency_simple(amount, currency)} - {safe_source}"
                        all_transactions.append(('💰', id, date, desc, safe_note))
                    
                    for exp in recent_expenses:
                        id, cat_name, amount, note, date, currency = exp
                        safe_cat = escape_markdown(cat_name)
                        safe_note = escape_markdown(note) if note else ""
                        desc = f"{format_currency_simple(amount, currency)} - {safe_cat}"
                        all_transactions.append(('💸', id, date, desc, safe_note))
                    
                    all_transactions.sort(key=lambda x: x[2], reverse=True)
                    
                    for emoji, id, date, desc, note in all_transactions[:20]:
                        msg += f"{emoji} #{id} {date}: {desc}\n"
                        if note:
                            msg += f"   📝 {note}\n"
                    
                    msg += f"\n🕐 {format_vn_time_short()}"
                    
                    # KIỂM TRA ĐỘ DÀI TIN NHẮN
                    if len(msg) > 4000:
                        await query.edit_message_text("📊 *Danh sách quá dài, đang chia nhỏ...*")
                        chunks = [msg[i:i+3500] for i in range(0, len(msg), 3500)]
                        for i, chunk in enumerate(chunks, 1):
                            await query.message.reply_text(
                                f"{chunk}\n\n*(Phần {i}/{len(chunks)})*",
                                parse_mode=ParseMode.MARKDOWN
                            )
                    else:
                        await query.edit_message_text(
                            msg, 
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                        )
                        
                except Exception as e:
                    logger.error(f"Lỗi expense_recent: {e}", exc_info=True)
                    await query.edit_message_text(
                        "❌ Có lỗi xảy ra!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                    )
            
            elif data == "export_csv" or data == "expense_export":
                await export_csv_handler(update, ctx)
                return

            elif data.startswith("balance_"):
                period = data.replace("balance_", "")
                current_user_id = query.from_user.id
                chat_id = query.message.chat.id
                effective_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
                
                if current_user_id != effective_user_id and not check_permission(chat_id, current_user_id, 'view'):
                    await query.edit_message_text("❌ Bạn không có quyền xem dữ liệu!")
                    return
                
                # Lấy thông tin user
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (effective_user_id,))
                user_info = c.fetchone()
                conn.close()
                
                user_name = f"@{user_info[0]}" if user_info and user_info[0] else (user_info[1] if user_info else "")
                
                # Tính cân đối
                balance_data = get_balance_summary(effective_user_id, period)
                
                if not balance_data:
                    await query.edit_message_text("❌ Không thể tính cân đối!")
                    return
                
                balance_msg = format_balance_message(balance_data, user_name)
                
                keyboard = [
                    [InlineKeyboardButton("📅 Hôm nay", callback_data="balance_day"),
                     InlineKeyboardButton("📅 Tháng này", callback_data="balance_month")],
                    [InlineKeyboardButton("📅 Năm nay", callback_data="balance_year"),
                     InlineKeyboardButton("📊 Tất cả", callback_data="balance_all")],
                    [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]
                ]
                
                await query.edit_message_text(
                    balance_msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif data == "expense_categories":
                owner_id = ctx.bot_data.get('effective_user_id', query.from_user.id)
                categories = get_expense_categories(owner_id)
                
                if not categories:
                    await query.edit_message_text(
                        f"📋 Chưa có danh mục nào!\nTạo: `dm [tên] [budget]`\n\n🕐 {format_vn_time_short()}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                    )
                    return
                
                msg = "📋 *DANH MỤC CỦA BẠN*\n━━━━━━━━━━━━━━━━\n\n"
                keyboard = []
                row = []
                
                for cat in categories:
                    cat_id, name, budget, created = cat
                    msg += f"• *{cat_id}.* {name} - {format_currency_simple(budget, 'VND')}\n"
                    
                    row.append(InlineKeyboardButton(f"🗑 {cat_id}", callback_data=f"del_cat_{cat_id}"))
                    if len(row) == 4:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                
                keyboard.append([InlineKeyboardButton("➕ Thêm danh mục", callback_data="expense_expense_menu"),
                                 InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")])
                
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await query.edit_message_text(
                    msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
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

    # ==================== WEBHOOK SETUP ====================
    async def setup_webhook():
        """Cấu hình webhook cho Render"""
        try:
            if not render_config.render_url:
                logger.warning("⚠️ Không có RENDER_EXTERNAL_URL, dùng polling")
                return False
            
            webhook_url = f"{render_config.render_url}/webhook"
            
            # Xóa webhook cũ
            await app.bot.delete_webhook(drop_pending_updates=True)
            
            # Set webhook mới
            await app.bot.set_webhook(
                url=webhook_url,
                allowed_updates=['message', 'callback_query'],
                drop_pending_updates=True,
                max_connections=render_config.get_worker_count()
            )
            
            webhook_info = await app.bot.get_webhook_info()
            logger.info(f"✅ Webhook set: {webhook_url}")
            logger.info(f"📊 Pending updates: {webhook_info.pending_update_count}")
            
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi setup webhook: {e}")
            return False

    # ==================== WEBHOOK HANDLER ====================
    @webhook_app.route('/webhook', methods=['POST'])
    def webhook():
        """Nhận updates từ Telegram"""
        try:
            update = Update.de_json(request.get_json(force=True), app.bot)
            asyncio.run_coroutine_threadsafe(
                app.process_update(update),
                app.loop
            )
            return 'OK', 200
        except Exception as e:
            logger.error(f"❌ Webhook error: {e}")
            return 'Error', 500

    @webhook_app.route('/health', methods=['GET'])
    def health():
        """Health check endpoint"""
        try:
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            
            # Kiểm tra database
            db_exists = os.path.exists(DB_PATH)
            db_size = os.path.getsize(DB_PATH) / 1024 if db_exists else 0
            
            status = {
                'status': 'healthy',
                'time': format_vn_time(),
                'uptime': time.time() - render_config.start_time,
                'memory_mb': round(memory_mb, 2),
                'db_size_kb': round(db_size, 2),
                'cache_stats': {
                    'price': price_cache.get_stats(),
                    'usdt': usdt_cache.get_stats()
                }
            }
            return json.dumps(status), 200, {'Content-Type': 'application/json'}
        except Exception as e:
            return json.dumps({'status': 'error', 'message': str(e)}), 500

    @webhook_app.route('/', methods=['GET'])
    def home():
        """Home page"""
        return f"""
        <html>
            <head><title>Crypto Bot</title></head>
            <body>
                <h1>🚀 Crypto & Expense Manager Bot</h1>
                <p>Status: <span style="color: green;">Running</span></p>
                <p>Time: {format_vn_time()}</p>
                <p>Uptime: {time.time() - render_config.start_time:.0f} seconds</p>
                <p><a href="/health">Health Check</a></p>
            </body>
        </html>
        """

    def run_webhook_server():
        """Chạy Flask server cho webhook"""
        port = int(os.environ.get('PORT', 10000))
        logger.info(f"🌐 Starting webhook server on port {port}")
        webhook_app.run(host='0.0.0.0', port=port, threaded=True)

    # ==================== ENHANCED HEALTH CHECK (HTTP Server) ====================
    class EnhancedHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                
                try:
                    import psutil
                    process = psutil.Process()
                    memory_mb = process.memory_info().rss / 1024 / 1024
                    cpu_percent = process.cpu_percent()
                    
                    db_size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
                    
                    status = {
                        'status': 'healthy',
                        'time': format_vn_time(),
                        'memory_mb': round(memory_mb, 2),
                        'cpu_percent': cpu_percent,
                        'db_size_kb': round(db_size, 2),
                        'cache_stats': {
                            'price': price_cache.get_stats(),
                            'usdt': usdt_cache.get_stats()
                        },
                        'uptime': time.time() - render_config.start_time
                    }
                    
                    self.wfile.write(json.dumps(status, indent=2).encode())
                except:
                    self.wfile.write(b'{"status": "healthy"}')
            
            elif self.path == '/metrics':
                self.send_response(200)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                
                try:
                    import psutil
                    process = psutil.Process()
                    memory_mb = process.memory_info().rss / 1024 / 1024
                    cpu_percent = process.cpu_percent()
                    db_size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
                    
                    metrics = f"""# HELP bot_memory Memory usage in MB
# TYPE bot_memory gauge
bot_memory {memory_mb}

# HELP bot_cpu CPU usage percent
# TYPE bot_cpu gauge
bot_cpu {cpu_percent}

# HELP bot_db_size Database size in KB
# TYPE bot_db_size gauge
bot_db_size {db_size}

# HELP bot_uptime Uptime in seconds
# TYPE bot_uptime counter
bot_uptime {time.time() - render_config.start_time}

# HELP bot_cache_hits Cache hit rate
# TYPE bot_cache_hits gauge
bot_cache_hits_price {price_cache.get_stats()['hit_rate']}
bot_cache_hits_usdt {usdt_cache.get_stats()['hit_rate']}
"""
                    self.wfile.write(metrics.encode())
                except:
                    self.wfile.write(b'# No metrics available')
            
            else:
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                html = f"""
                <html>
                    <head><title>Crypto Bot</title></head>
                    <body>
                        <h1>🚀 Crypto & Expense Manager Bot</h1>
                        <p>Status: <span style="color: green;">Running</span></p>
                        <p>Time: {format_vn_time()}</p>
                        <p>Uptime: {time.time() - render_config.start_time:.0f} seconds</p>
                        <p>
                            <a href="/health">Health Check (JSON)</a> | 
                            <a href="/metrics">Metrics (Prometheus)</a>
                        </p>
                    </body>
                </html>
                """
                self.wfile.write(html.encode())
        
        def log_message(self, format, *args):
            return

    def run_health_server():
        """Chạy HTTP server cho health check (fallback)"""
        try:
            port = int(os.environ.get('PORT', 10000))
            server = HTTPServer(('0.0.0.0', port), EnhancedHealthHandler)
            logger.info(f"✅ Health server on port {port}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"❌ Health server error: {e}")
            time.sleep(10)

    # ==================== SMART STARTUP ====================
    def smart_startup():
        """Khởi động thông minh tùy theo môi trường"""
        logger.info("🚀 SMART STARTUP")
        logger.info(f"📊 Render mode: {render_config.is_render}")
        logger.info(f"💾 Memory limit: {render_config.memory_limit}MB")
        logger.info(f"⚙️ CPU limit: {render_config.cpu_limit}")
        logger.info(f"🌐 Render URL: {render_config.render_url}")

        # Kiểm tra và tạo thư mục export
        EXPORT_DIR = os.path.join(DATA_DIR, 'exports')
        os.makedirs(EXPORT_DIR, exist_ok=True)
        logger.info(f"📁 Export directory: {EXPORT_DIR}")
        
        # Kiểm tra quyền ghi file
        test_file = os.path.join(EXPORT_DIR, 'test.txt')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            logger.info("✅ Export directory is writable")
        except Exception as e:
            logger.error(f"❌ Export directory not writable: {e}")
        
        # Khởi tạo database
        if not init_database():
            logger.error("❌ KHÔNG THỂ KHỞI TẠO DATABASE")
            time.sleep(5)
            
        auto_migrate_permissions()
        migrate_permissions_table()

        # Sau khi init_database
        load_group_owners()

        # Migrate database
        try:
            migrate_database()
        except Exception as e:
            logger.error(f"❌ Lỗi migrate: {e}")
        
        # Optimize database lúc khởi động
        optimize_database()
        
        # Chọn chế độ chạy
        if render_config.is_render and render_config.render_url:
            logger.info("🌐 Using webhook mode")
            # Setup webhook
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(setup_webhook())
            
            # Chạy Flask webhook server
            threading.Thread(target=run_webhook_server, daemon=True).start()
        else:
            logger.info("🔄 Using polling mode")
            # Chạy health check server
            threading.Thread(target=run_health_server, daemon=True).start()
        
        # Background threads
        threading.Thread(target=memory_monitor, daemon=True).start()
        threading.Thread(target=schedule_backup, daemon=True).start()
        threading.Thread(target=check_alerts, daemon=True).start()
        
        logger.info(f"🎉 BOT ĐÃ SẴN SÀNG! {format_vn_time()}")

    # ==================== MAIN ====================
    if __name__ == '__main__':
        try:
            logger.info("🚀 KHỞI ĐỘNG CRYPTO BOT - RENDER OPTIMIZED")
            logger.info(f"🕐 Thời gian: {format_vn_time()}")
            
            # Tạo application
            app = Application.builder().token(TELEGRAM_TOKEN).build()
            app.bot_data = {}
            logger.info("✅ Đã tạo Telegram Application")
            
            # Đăng ký handlers
            app.add_handler(CommandHandler("start", start))
            app.add_handler(CommandHandler("help", help_command))
            app.add_handler(CommandHandler("menu", menu_command))
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
            app.add_handler(CommandHandler("perm", perm_command))
            app.add_handler(CommandHandler("whoami", whoami_command))
            app.add_handler(CommandHandler("permgrant", quick_grant_command))
            app.add_handler(CommandHandler("getid", getid_command))
            app.add_handler(CommandHandler("syncusers", sync_users_command))
            app.add_handler(CommandHandler("view", view_portfolio_command))
            app.add_handler(CommandHandler("users", list_users_command))
            app.add_handler(CommandHandler("syncadmins", sync_admins_command))
            app.add_handler(CommandHandler("checkperm", check_perm_command))
            app.add_handler(CommandHandler("syncdata", sync_data_command))
            app.add_handler(CommandHandler("owner", owner_panel))
            app.add_handler(CommandHandler("debugperm", debug_perm_command))
            app.add_handler(CommandHandler("setupgroup", setup_group_command))
            app.add_handler(CommandHandler("groupinfo", group_info_command))
            app.add_handler(CommandHandler("addadmin", add_group_admin))
            app.add_handler(CommandHandler("hide", hide_keyboard))
            app.add_handler(CommandHandler("balance", balance_command))
            app.add_handler(CommandHandler("canhdoi", balance_command))
            app.add_handler(CommandHandler("thuchi", balance_command))
            app.add_handler(CommandHandler("addadmin", add_admin_command))
            app.add_handler(CommandHandler("listadmin", list_admin_command))
            app.add_handler(CommandHandler("removeadmin", remove_admin_command))
            app.add_handler(CommandHandler("xoadm", delete_category_command))
            app.add_handler(CommandHandler("xoacategory", delete_category_command))
            app.add_handler(CommandHandler("xoadanhmuc", delete_category_command))
            app.add_handler(CommandHandler("xoadanhmuc", delete_category_command))
            app.add_handler(CommandHandler("delcat", delete_category_command))
            app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_members))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_handler(CallbackQueryHandler(handle_callback))
            
            logger.info("✅ Đã đăng ký handlers")
            
            # Khởi động thông minh
            smart_startup()
            
            # Chạy bot
            if render_config.is_render and render_config.render_url:
                # Webhook mode: Flask đã chạy, cần giữ main thread alive
                logger.info("⏳ Bot running in webhook mode...")
                while True:
                    time.sleep(60)
                    check_memory_usage()
            else:
                # Polling mode
                logger.info("⏳ Bot running in polling mode...")
                app.run_polling(timeout=30, drop_pending_updates=True)
            
        except Exception as e:
            logger.error(f"❌ LỖI: {e}", exc_info=True)
            time.sleep(5)
            # Thử restart lại bot
            try:
                os.execv(sys.executable, ['python'] + sys.argv)
            except Exception as restart_error:
                logger.error(f"❌ Không thể restart: {restart_error}")
                time.sleep(60)
