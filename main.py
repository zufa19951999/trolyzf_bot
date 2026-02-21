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
    if not text:
        return ""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text
    
# ==================== THÊM HÀM NÀY VÀO ĐÂY ====================
async def safe_edit_message(query, text, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    """Sửa message an toàn, tự động escape nếu có lỗi Markdown"""
    try:
        # Thử gửi với Markdown
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    except Exception as e:
        # Log lỗi để debug
        logger.warning(f"⚠️ Lỗi Markdown trong callback: {e}")
        logger.warning(f"📝 Text gây lỗi (độ dài {len(text)}): {text[:100]}...")
        
        # KIỂM TRA NẾU TEXT CHỨA #cat_ THÌ THAY THẾ
        if "#cat_" in text:
            logger.warning("🚫 Phát hiện #cat_ trong text, thay bằng message mặc định")
            text = "❌ Có lỗi xảy ra, vui lòng thử lại sau."
        
        # Cách 1: Thử gửi không Markdown
        try:
            await query.edit_message_text(text, parse_mode=None, reply_markup=reply_markup)
            logger.info("✅ Đã sửa bằng cách gửi không Markdown")
            return
        except Exception as e2:
            logger.warning(f"⚠️ Vẫn lỗi khi gửi không Markdown: {e2}")
        
        # Cách 2: Gửi message mặc định
        try:
            await query.edit_message_text("❌ Có lỗi hiển thị, vui lòng thử lại sau.", parse_mode=None)
        except:
            pass
            
# ==================== OWNER CONFIGURATION ====================
OWNER_ID = 1164334777
OWNER_USERNAME = "adm"

def is_owner(user_id):
    return user_id == OWNER_ID

# ==================== GROUP OWNER MANAGEMENT ====================
GROUP_OWNERS = {}

def load_group_owners():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT group_id, owner_id FROM group_owners")
        rows = c.fetchall()
        for group_id, owner_id in rows:
            GROUP_OWNERS[group_id] = owner_id
        conn.close()
        logger.info(f"✅ Loaded {len(GROUP_OWNERS)} group owners from DB")
    except Exception as e:
        logger.error(f"❌ Lỗi load group owners: {e}")
        
def set_group_owner(group_id, owner_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
        c.execute('''INSERT OR REPLACE INTO group_owners (group_id, owner_id, created_at) VALUES (?, ?, ?)''', (group_id, owner_id, created_at))
        conn.commit()
        conn.close()
        GROUP_OWNERS[group_id] = owner_id
        logger.info(f"✅ Set owner {owner_id} for group {group_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Lỗi set group owner: {e}")
        return False

def get_group_owner(group_id):
    """Lấy owner_id của group, ưu tiên từ RAM, nếu không có thì đọc từ DB"""
    # Kiểm tra trong RAM trước
    owner_id = GROUP_OWNERS.get(group_id)
    if owner_id:
        return owner_id
    
    # Nếu không có trong RAM, đọc từ database
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT owner_id FROM group_owners WHERE group_id = ?", (group_id,))
        result = c.fetchone()
        conn.close()
        
        if result:
            owner_id = result[0]
            # Lưu lại vào RAM cho lần sau
            GROUP_OWNERS[group_id] = owner_id
            logger.info(f"✅ Loaded owner {owner_id} for group {group_id} from DB")
            return owner_id
    except Exception as e:
        logger.error(f"❌ Lỗi đọc group owner từ DB: {e}")
    
    # Fallback về OWNER_ID
    return OWNER_ID
    
def load_group_owner(group_id):
    """Load một group cụ thể vào RAM"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT owner_id FROM group_owners WHERE group_id = ?", (group_id,))
        result = c.fetchone()
        conn.close()
        
        if result:
            GROUP_OWNERS[group_id] = result[0]
            return result[0]
    except Exception as e:
        logger.error(f"❌ Lỗi load group owner {group_id}: {e}")
    
    return None
    
def is_group_owner(group_id, user_id):
    return user_id == get_group_owner(group_id)

def get_effective_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id
    
    if chat_type in ['group', 'supergroup']:
        group_id = update.effective_chat.id
        owner_id = get_group_owner(group_id)
        
        if not owner_id:
            logger.warning(f"⚠️ Group {group_id} chưa có owner")
            return None, user_id
        
        logger.info(f"🏢 Group {group_id}: user {user_id} đang thao tác trên data của owner {owner_id}")
        return owner_id, user_id
    
    logger.info(f"💬 Private: user {user_id} tự quản lý data riêng")
    return user_id, user_id

# ==================== USERNAME CACHE ====================
class UsernameCache:
    def __init__(self):
        self.cache = {}
        self.last_update = {}
        self.ttl = 3600
    
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
        self.memory_limit = int(os.environ.get('MEMORY_LIMIT', 512))
        self.cpu_limit = float(os.environ.get('CPU_LIMIT', 1))
        self.render_url = os.environ.get('RENDER_EXTERNAL_URL')
        self.start_time = time.time()
        
    def get_worker_count(self):
        if self.is_render:
            return max(1, int(self.cpu_limit) * 2)
        return 4
    
    def should_cleanup(self):
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
        if len(self.cache) >= self.max_size:
            oldest = min(self.cache.keys(), key=lambda k: self.cache[k][1])
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

price_cache = AdvancedCache('price', max_size=50, ttl=60)
usdt_cache = AdvancedCache('usdt', max_size=1, ttl=180)

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

# ==================== PERMISSION DECORATORS ====================
def require_permission(permission_type):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            chat_type = update.effective_chat.type
            
            # Owner bot luôn có quyền
            if is_owner(user_id):
                return await func(update, context, *args, **kwargs)
            
            # PRIVATE CHAT: Ai cũng dùng được
            if chat_type == 'private':
                return await func(update, context, *args, **kwargs)
            
            # TRONG GROUP: Kiểm tra quyền nghiêm ngặt
            if chat_type in ['group', 'supergroup']:
                # Kiểm tra xem user có được cấp quyền không
                if not check_permission(chat_id, user_id, permission_type):
                    await update.message.reply_text(
                        "❌ *KHÔNG CÓ QUYỀN SỬ DỤNG BOT TRONG NHÓM*\n\n"
                        "Bạn chưa được cấp quyền sử dụng bot trong nhóm này.\n"
                        "Vui lòng liên hệ chủ sở hữu nhóm để được cấp quyền.\n\n"
                        "💡 Trong chat riêng với bot, bạn vẫn có thể sử dụng bình thường.\n\n"
                        f"🕐 {format_vn_time()}", 
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
                
                # Nếu có quyền, cho phép sử dụng
                return await func(update, context, *args, **kwargs)
            
            # Các loại chat khác (channel, v.v.)
            return await func(update, context, *args, **kwargs)
            
        return wrapper
    return decorator

def require_group_permission(permission_type):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            chat_type = update.effective_chat.type
            
            # Owner bot luôn có quyền
            if is_owner(user_id):
                return await func(update, context, *args, **kwargs)
            
            # Lệnh này chỉ dùng trong group
            if chat_type not in ['group', 'supergroup']:
                await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
                return
            
            # Trong group, kiểm tra quyền
            if not check_permission(chat_id, user_id, permission_type):
                await update.message.reply_text(
                    "❌ *KHÔNG CÓ QUYỀN THỰC HIỆN LỆNH NÀY*\n\n"
                    "Bạn không có quyền sử dụng lệnh này trong nhóm.\n\n"
                    f"🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
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
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("VACUUM")
            c.execute('''DELETE FROM alerts WHERE triggered_at IS NOT NULL AND date(triggered_at) < date('now', '-30 days')''')
            conn.commit()
            conn.close()
            
            if os.path.exists('bot.log'):
                with open('bot.log', 'r') as f:
                    lines = f.readlines()
                if len(lines) > 1000:
                    with open('bot.log', 'w') as f:
                        f.writelines(lines[-1000:])
            
            size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
            logger.info(f"✅ Database optimized: {size_mb:.2f}MB")
        except Exception as e:
            logger.error(f"❌ Lỗi optimize DB: {e}")

    # ==================== MEMORY MONITOR ====================
    def check_memory_usage():
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024
            cpu_percent = process.cpu_percent()
            
            logger.info(f"📊 Memory: {memory_mb:.2f}MB | CPU: {cpu_percent:.1f}% | Cache: P{price_cache.get_stats()['size']}/U{usdt_cache.get_stats()['size']}")
            
            if memory_mb > render_config.memory_limit * 0.7:
                logger.warning("⚠️ Memory high, cleaning caches...")
                price_cache.clear()
                usdt_cache.clear()
                gc.collect()
                
            if memory_mb > render_config.memory_limit * 0.9:
                logger.critical("💥 Memory critical, restarting...")
                sys.exit(1)
        except Exception as e:
            logger.error(f"❌ Memory check error: {e}")

    def memory_monitor():
        while True:
            check_memory_usage()
            time.sleep(300)

    # ==================== DATABASE SETUP ====================
    def init_database():
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS portfolio (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, symbol TEXT, amount REAL, buy_price REAL, buy_date TEXT, total_cost REAL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, symbol TEXT, target_price REAL, condition TEXT, is_active INTEGER DEFAULT 1, created_at TEXT, triggered_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS expense_categories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, budget REAL, created_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category_id INTEGER, amount REAL, currency TEXT DEFAULT 'VND', note TEXT, expense_date TEXT, created_at TEXT, FOREIGN KEY (category_id) REFERENCES expense_categories(id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS incomes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, currency TEXT DEFAULT 'VND', source TEXT, income_date TEXT, note TEXT, created_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, last_seen TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS group_admins (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, admin_id INTEGER, granted_by INTEGER, can_view INTEGER DEFAULT 0, can_edit INTEGER DEFAULT 0, can_delete INTEGER DEFAULT 0, can_manage INTEGER DEFAULT 0, created_at TEXT, UNIQUE(group_id, admin_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS permission_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, action_by INTEGER, target_user INTEGER, action TEXT, old_role TEXT, new_role TEXT, created_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS group_owners (group_id INTEGER PRIMARY KEY, owner_id INTEGER, created_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS permissions (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER, granted_by INTEGER, is_approved INTEGER DEFAULT 1, role TEXT DEFAULT 'user', can_view_all INTEGER DEFAULT 0, can_edit_all INTEGER DEFAULT 0, can_delete_all INTEGER DEFAULT 0, can_manage_perms INTEGER DEFAULT 0, created_at TEXT, approved_at TEXT, UNIQUE(group_id, user_id))''')
            
            conn.commit()
            logger.info(f"✅ Database initialized")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi database: {e}")
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
            logger.error(f"❌ Lỗi migrate: {e}")
        finally:
            if conn:
                conn.close()

    def backup_database():
        try:
            if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 1024 * 1024:
                timestamp = get_vn_time().strftime('%Y%m%d_%H%M%S')
                backup_path = os.path.join(BACKUP_DIR, f'backup_{timestamp}.db')
                shutil.copy2(DB_PATH, backup_path)
                
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
                time.sleep(86400)
            except:
                time.sleep(3600)

    # ==================== BATCH PRICE FETCHING ====================
    def get_prices_batch(symbols):
        try:
            if not CMC_API_KEY or not symbols:
                return {}
            
            results = {}
            uncached = []
            
            for symbol in symbols:
                cached = price_cache.get(symbol)
                if cached:
                    results[symbol] = cached
                else:
                    uncached.append(symbol)
            
            if uncached:
                for i in range(0, len(uncached), 10):
                    batch = uncached[i:i+10]
                    symbols_str = ','.join(batch)
                    
                    headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY}
                    params = {'symbol': symbols_str, 'convert': 'USD'}
                    
                    res = requests.get(f"{CMC_API_URL}/cryptocurrency/quotes/latest", headers=headers, params=params, timeout=10)
                    
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
                    
                    time.sleep(0.5)
            
            return results
        except Exception as e:
            logger.error(f"❌ Batch price error: {e}")
            return {}

    def get_price(symbol):
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
            
            res = requests.get(f"{CMC_API_URL}/cryptocurrency/quotes/latest", headers=headers, params=params, timeout=10)
            
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
        cached = usdt_cache.get('rate')
        if cached:
            return cached
        
        try:
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
            
            c.execute('''INSERT INTO portfolio (user_id, symbol, amount, buy_price, buy_date, total_cost) VALUES (?, ?, ?, ?, ?, ?)''',
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
            c.execute('''SELECT symbol, amount, buy_price, buy_date, total_cost FROM portfolio WHERE user_id = ? ORDER BY buy_date''', (user_id,))
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
                        FROM portfolio WHERE user_id = ? ORDER BY buy_date''', (user_id,))
            transactions = c.fetchall()
            
            # THÊM LOG ĐỂ DEBUG
            logger.info(f"🔍 get_transaction_detail: user_id={user_id}, found={len(transactions)} transactions")
            for tx in transactions:
                logger.info(f"   • #{tx[0]}: {tx[1]} {tx[2]} @ {tx[3]}")
                
            return transactions
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
            c.execute('''DELETE FROM portfolio WHERE id = ? AND user_id = ?''', (transaction_id, user_id))
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
            
            c.execute('''INSERT INTO alerts (user_id, symbol, target_price, condition, created_at) VALUES (?, ?, ?, ?, ?)''',
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
            c.execute('''SELECT id, symbol, target_price, condition, created_at FROM alerts WHERE user_id = ? AND is_active = 1 ORDER BY created_at''', (user_id,))
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
                c.execute('''SELECT id, user_id, symbol, target_price, condition FROM alerts WHERE is_active = 1''')
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
                            c.execute('''UPDATE alerts SET is_active = 0, triggered_at = ? WHERE id = ?''', 
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
            
            c.execute("DELETE FROM permissions WHERE group_id = ? AND user_id = ?", (group_id, user_id))
            
            c.execute('''INSERT INTO permissions (group_id, user_id, granted_by, is_approved, role, can_view_all, can_edit_all, can_delete_all, can_manage_perms, created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (group_id, user_id, granted_by, 1, 'staff',
                       permissions.get('view', 0), permissions.get('edit', 0),
                       permissions.get('delete', 0), permissions.get('manage', 0),
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
            c.execute("DELETE FROM permissions WHERE group_id = ? AND user_id = ?", (group_id, user_id))
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

    def check_permission(group_id, user_id, permission_type='view'):
        """Kiểm tra quyền của user trong group"""
        conn = None
        try:
            # Owner bot luôn có quyền
            if is_owner(user_id):
                return True
            
            # Chủ sở hữu group luôn có quyền
            owner_id = get_group_owner(group_id)
            if user_id == owner_id:
                return True
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            c.execute('''SELECT can_view_all, can_edit_all, can_delete_all, can_manage_perms 
                        FROM permissions WHERE group_id = ? AND user_id = ?''', 
                        (group_id, user_id))
            result = c.fetchone()
            
            if not result:
                return False
            
            can_view, can_edit, can_delete, can_manage = result
            
            # Admin có quyền cao hơn user thường
            if permission_type == 'view':
                return can_view == 1 or can_edit == 1 or can_delete == 1 or can_manage == 1
            elif permission_type == 'edit':
                return can_edit == 1 or can_manage == 1  # Manage cũng có quyền edit
            elif permission_type == 'delete':
                return can_delete == 1 or can_manage == 1  # Manage cũng có quyền delete
            elif permission_type == 'manage':
                return can_manage == 1
            
            return False
        except Exception as e:
            logger.error(f"❌ Lỗi check_permission: {e}")
            return False
        finally:
            if conn:
                conn.close()


    def check_user_access(group_id, user_id, required_role='user'):
        try:
            if is_owner(user_id):
                return True
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            c.execute('''SELECT role, is_approved, can_view_all, can_edit_all, can_delete_all, can_manage_perms FROM permissions WHERE group_id = ? AND user_id = ?''', (group_id, user_id))
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

    def grant_user_access(group_id, target_user_id, granted_by, role='user'):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute("DELETE FROM permissions WHERE group_id = ? AND user_id = ?", (group_id, target_user_id))
            
            if role == 'staff':
                permissions = {'is_approved': 1, 'role': 'staff', 'view': 1, 'edit': 1, 'delete': 1, 'manage': 0}
            else:
                permissions = {'is_approved': 1, 'role': 'user', 'view': 1, 'edit': 0, 'delete': 0, 'manage': 0}
            
            c.execute('''INSERT INTO permissions (group_id, user_id, granted_by, is_approved, role, can_view_all, can_edit_all, can_delete_all, can_manage_perms, created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (group_id, target_user_id, granted_by,
                       permissions['is_approved'], permissions['role'],
                       permissions['view'], permissions['edit'],
                       permissions['delete'], permissions['manage'],
                       created_at, created_at))
            
            c.execute('''INSERT INTO permission_logs (group_id, action_by, target_user, action, new_role, created_at) VALUES (?, ?, ?, ?, ?, ?)''',
                      (group_id, granted_by, target_user_id, 'GRANT', role, created_at))
            
            conn.commit()
            conn.close()
            
            logger.info(f"✅ Granted {role} access to user {target_user_id} in group {group_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi grant_user_access: {e}")
            return False

    def migrate_admin_data():
        """Di chuyển dữ liệu admin từ bảng cũ sang bảng mới"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Kiểm tra xem bảng group_admins có dữ liệu không
            c.execute("SELECT COUNT(*) FROM group_admins")
            old_admin_count = c.fetchone()[0]
            
            if old_admin_count > 0:
                logger.info(f"🔄 Migrating {old_admin_count} old admin records...")
                
                # Lấy tất cả admin cũ
                c.execute('''
                    SELECT group_id, admin_id, granted_by, can_view, can_edit, can_delete, can_manage, created_at 
                    FROM group_admins
                ''')
                old_admins = c.fetchall()
                
                migrated = 0
                for admin in old_admins:
                    group_id, admin_id, granted_by, can_view, can_edit, can_delete, can_manage, created_at = admin
                    
                    # Kiểm tra xem đã có trong bảng permissions chưa
                    c.execute('''SELECT id FROM permissions WHERE group_id = ? AND user_id = ?''', (group_id, admin_id))
                    if not c.fetchone():
                        # Thêm vào bảng permissions
                        c.execute('''
                            INSERT INTO permissions 
                            (group_id, user_id, granted_by, is_approved, role, 
                             can_view_all, can_edit_all, can_delete_all, can_manage_perms, created_at, approved_at) 
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            group_id, admin_id, granted_by, 1, 'staff',
                            can_view, can_edit, can_delete, can_manage,
                            created_at, created_at
                        ))
                        migrated += 1
                
                conn.commit()
                logger.info(f"✅ Migrated {migrated} admin records to permissions table")
            
            conn.close()
        except Exception as e:
            logger.error(f"❌ Lỗi migrate admin data: {e}")

    # ==================== USER FUNCTIONS WITH AUTO-UPDATE ====================
    async def update_user_info_async(user):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            current_time = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
            exists = c.fetchone()
            
            if exists:
                c.execute('''UPDATE users SET username = ?, first_name = ?, last_name = ?, last_seen = ? WHERE user_id = ?''',
                          (user.username, user.first_name, user.last_name, current_time, user.id))
            else:
                c.execute('''INSERT INTO users (user_id, username, first_name, last_name, last_seen) VALUES (?, ?, ?, ?, ?)''',
                          (user.id, user.username, user.first_name, user.last_name, current_time))
            
            conn.commit()
            conn.close()
            
            if user.username:
                username_cache.set(user.username, user.id)
            
            logger.info(f"✅ Updated user {user.id} (@{user.username})")
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi cập nhật user {user.id}: {e}")
            return False

    def get_user_id_by_username(username):
        conn = None
        try:
            clean_username = username.lower().replace('@', '').strip()
            
            cached_id = username_cache.get(clean_username)
            if cached_id:
                logger.info(f"Cache hit for @{clean_username}: {cached_id}")
                return cached_id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            c.execute("SELECT user_id FROM users WHERE username = ?", (clean_username,))
            result = c.fetchone()
            
            if result:
                user_id = result[0]
                username_cache.set(clean_username, user_id)
                return user_id
            
            c.execute("SELECT user_id, username FROM users WHERE username LIKE ?", (f"%{clean_username}%",))
            results = c.fetchall()
            
            if results:
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

    def auto_update_user(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            if update.effective_user:
                await update_user_info_async(update.effective_user)
            
            chat_type = update.effective_chat.type
            current_user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            
            # ===== QUAN TRỌNG: XÓA SẠCH DỮ LIỆU CŨ =====
            # Xóa tất cả biến liên quan đến quyền trong context
            keys_to_remove = ['effective_user_id', 'is_admin', 'is_owner', 'group_owner_id']
            for key in keys_to_remove:
                if key in context.bot_data:
                    del context.bot_data[key]
            
            # PRIVATE CHAT: LUÔN TỰ QUẢN LÝ, KHÔNG BAO GIỜ LÀ ADMIN
            if chat_type == 'private':
                context.bot_data['effective_user_id'] = current_user_id
                context.bot_data['current_user_id'] = current_user_id
                context.bot_data['chat_type'] = chat_type
                context.bot_data['is_admin'] = False  # LUÔN FALSE
                context.bot_data['is_owner'] = False
                logger.info(f"💬 PRIVATE CHAT: user {current_user_id} tự quản lý (KHÔNG phải admin)")
                return await func(update, context, *args, **kwargs)
            
            # TRONG GROUP
            elif chat_type in ['group', 'supergroup']:
                owner_id = get_group_owner(chat_id)
                
                if not owner_id:
                    await update.message.reply_text(
                        f"❌ *GROUP CHƯA ĐƯỢC CẤU HÌNH*\n\n"
                        f"Vui lòng liên hệ @{OWNER_USERNAME} để thiết lập.\n\n"
                        f"🕐 {format_vn_time()}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
                
                # Lưu owner_id để dùng sau
                context.bot_data['group_owner_id'] = owner_id
                
                # Kiểm tra quyền trong group
                has_permission = check_permission(chat_id, current_user_id, 'view')
                
                if not has_permission:
                    # User không có quyền: vẫn cho phép nhưng tự quản lý
                    context.bot_data['effective_user_id'] = current_user_id
                    context.bot_data['current_user_id'] = current_user_id
                    context.bot_data['chat_type'] = chat_type
                    context.bot_data['is_admin'] = False
                    context.bot_data['is_owner'] = (current_user_id == owner_id)
                    logger.info(f"👤 GROUP: user {current_user_id} chưa có quyền, tự quản lý")
                    return await func(update, context, *args, **kwargs)
                
                # Kiểm tra quyền admin
                is_admin = check_permission(chat_id, current_user_id, 'edit') or \
                          check_permission(chat_id, current_user_id, 'delete') or \
                          check_permission(chat_id, current_user_id, 'manage')
                
                if is_admin or current_user_id == owner_id:
                    # Admin hoặc owner: thao tác trên dữ liệu của owner
                    context.bot_data['effective_user_id'] = owner_id
                    context.bot_data['is_admin'] = True
                    context.bot_data['is_owner'] = (current_user_id == owner_id)
                    logger.info(f"👑 GROUP: admin {current_user_id} thao tác trên dữ liệu owner {owner_id}")
                else:
                    # User thường có quyền view: tự quản lý
                    context.bot_data['effective_user_id'] = current_user_id
                    context.bot_data['is_admin'] = False
                    context.bot_data['is_owner'] = False
                    logger.info(f"👤 GROUP: user {current_user_id} có quyền view, tự quản lý")
                
                context.bot_data['current_user_id'] = current_user_id
                context.bot_data['chat_type'] = chat_type
                
                return await func(update, context, *args, **kwargs)
            
            # Các loại chat khác
            else:
                context.bot_data['effective_user_id'] = current_user_id
                context.bot_data['current_user_id'] = current_user_id
                context.bot_data['chat_type'] = chat_type
                context.bot_data['is_admin'] = False
                context.bot_data['is_owner'] = False
                return await func(update, context, *args, **kwargs)
                
        return wrapper
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
            
            c.execute('''INSERT INTO expense_categories (user_id, name, budget, created_at) VALUES (?, ?, ?, ?)''',
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
            c.execute('''SELECT id, name, budget, created_at FROM expense_categories WHERE user_id = ? ORDER BY name''', (owner_id,))
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
            
            c.execute('''INSERT INTO incomes (user_id, amount, source, income_date, note, created_at, currency) VALUES (?, ?, ?, ?, ?, ?, ?)''',
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
            
            c.execute('''INSERT INTO expenses (user_id, category_id, amount, note, expense_date, created_at, currency) VALUES (?, ?, ?, ?, ?, ?, ?)''',
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
            c.execute('''SELECT id, amount, source, note, income_date, currency FROM incomes WHERE user_id = ? ORDER BY income_date DESC, created_at DESC LIMIT ?''', (user_id, limit))
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
            c.execute('''SELECT e.id, ec.name, e.amount, e.note, e.expense_date, e.currency FROM expenses e JOIN expense_categories ec ON e.category_id = ec.id WHERE e.user_id = ? ORDER BY e.expense_date DESC, e.created_at DESC LIMIT ?''', (user_id, limit))
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
                query = '''SELECT id, amount, source, note, currency, income_date FROM incomes WHERE user_id = ? AND income_date = ? ORDER BY income_date DESC, created_at DESC'''
                c.execute(query, (user_id, date_filter))
            elif period == 'month':
                month_filter = now.strftime("%Y-%m")
                query = '''SELECT id, amount, source, note, currency, income_date FROM incomes WHERE user_id = ? AND strftime('%Y-%m', income_date) = ? ORDER BY income_date DESC, created_at DESC'''
                c.execute(query, (user_id, month_filter))
            else:
                year_filter = now.strftime("%Y")
                query = '''SELECT id, amount, source, note, currency, income_date FROM incomes WHERE user_id = ? AND strftime('%Y', income_date) = ? ORDER BY income_date DESC, created_at DESC'''
                c.execute(query, (user_id, year_filter))
            
            rows = c.fetchall()
            
            summary = {}
            for row in rows:
                id, amount, source, note, currency, date = row
                if currency not in summary:
                    summary[currency] = 0
                summary[currency] += amount
            
            return {'transactions': rows, 'summary': summary, 'total_count': len(rows)}
        except Exception as e:
            logger.error(f"❌ Lỗi income summary: {e}")
            return {'transactions': [], 'summary': {}, 'total_count': 0}
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
                query = '''SELECT e.id, ec.name, e.amount, e.note, e.currency, e.expense_date, ec.budget FROM expenses e JOIN expense_categories ec ON e.category_id = ec.id WHERE e.user_id = ? AND e.expense_date = ? ORDER BY e.expense_date DESC, e.created_at DESC'''
                c.execute(query, (user_id, date_filter))
            elif period == 'month':
                month_filter = now.strftime("%Y-%m")
                query = '''SELECT e.id, ec.name, e.amount, e.note, e.currency, e.expense_date, ec.budget FROM expenses e JOIN expense_categories ec ON e.category_id = ec.id WHERE e.user_id = ? AND strftime('%Y-%m', e.expense_date) = ? ORDER BY e.expense_date DESC, e.created_at DESC'''
                c.execute(query, (user_id, month_filter))
            else:
                year_filter = now.strftime("%Y")
                query = '''SELECT e.id, ec.name, e.amount, e.note, e.currency, e.expense_date, ec.budget FROM expenses e JOIN expense_categories ec ON e.category_id = ec.id WHERE e.user_id = ? AND strftime('%Y', e.expense_date) = ? ORDER BY e.expense_date DESC, e.created_at DESC'''
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
            
            return {'transactions': rows, 'summary': summary, 'category_summary': category_summary, 'total_count': len(rows)}
        except Exception as e:
            logger.error(f"❌ Lỗi expenses summary: {e}")
            return {'transactions': [], 'summary': {}, 'category_summary': {}, 'total_count': 0}
        finally:
            if conn:
                conn.close()

    def get_balance_summary(user_id, period='month'):
        try:
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
            else:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                
                c.execute('''SELECT currency, SUM(amount) FROM incomes WHERE user_id = ? GROUP BY currency''', (user_id,))
                income_rows = c.fetchall()
                
                c.execute('''SELECT currency, SUM(amount) FROM expenses WHERE user_id = ? GROUP BY currency''', (user_id,))
                expense_rows = c.fetchall()
                
                conn.close()
                
                incomes = {'summary': {}}
                expenses = {'summary': {}}
                
                for currency, total in income_rows:
                    incomes['summary'][currency] = total
                
                for currency, total in expense_rows:
                    expenses['summary'][currency] = total
                
                title = "TỔNG KẾT TẤT CẢ"
            
            # Lấy tất cả các loại tiền tệ
            all_currencies = set(list(incomes['summary'].keys()) + list(expenses['summary'].keys()))
            
            balance_data = []
            
            for currency in all_currencies:
                income = incomes['summary'].get(currency, 0)
                expense = expenses['summary'].get(currency, 0)
                balance = income - expense
                
                balance_data.append({
                    'currency': currency,
                    'income': income,
                    'expense': expense,
                    'balance': balance,
                    'status': 'positive' if balance > 0 else 'negative' if balance < 0 else 'zero'
                })
            
            return {
                'title': title,
                'period': period,
                'balances': balance_data,
                'income_count': incomes.get('total_count', 0),
                'expense_count': expenses.get('total_count', 0)
            }
        except Exception as e:
            logger.error(f"❌ Lỗi get_balance_summary: {e}")
            return None

    def format_balance_message(balance_data, user_name=""):
        if not balance_data:
            return "❌ Không có dữ liệu để hiển thị!"
        
        # Icon và tên cho các loại tiền tệ
        currency_icons = {
            'VND': '🇻🇳',
            'USD': '🇺🇸',
            'USDT': '💵',
            'KHR': '🇰🇭',
            'LKR': '🇱🇰'
        }
        
        msg = f"⚖️ *CÂN ĐỐI THU CHI - {balance_data['title']}*"
        if user_name:
            msg += f" - {user_name}"
        msg += "\n━━━━━━━━━━━━━━━━\n\n"
        
        # Hiển thị theo từng loại tiền tệ
        for b in balance_data['balances']:
            currency = b['currency']
            income = b['income']
            expense = b['expense']
            balance = b['balance']
            
            # Icon cho loại tiền
            icon = currency_icons.get(currency, '💱')
            
            # Header cho loại tiền
            msg += f"{icon} *{currency}*\n"
            msg += "```\n"
            
            # Thu nhập
            if income > 0:
                msg += f"💰 Thu:    {format_currency_simple(income, currency):>15}\n"
            else:
                msg += f"💰 Thu:    {'0':>15}\n"
            
            # Chi tiêu
            if expense > 0:
                msg += f"💸 Chi:    {format_currency_simple(expense, currency):>15}\n"
            else:
                msg += f"💸 Chi:    {'0':>15}\n"
            
            # Đường kẻ
            msg += f"{'─'*25}\n"
            
            # Cân đối
            if balance > 0:
                msg += f"✅ Dư:     {format_currency_simple(balance, currency):>15}\n"
            elif balance < 0:
                msg += f"❌ Thiếu:  {format_currency_simple(abs(balance), currency):>15}\n"
            else:
                msg += f"➖ Cân bằng: {'0':>15}\n"
            
            msg += "```\n"
        
        # Thống kê số giao dịch
        msg += f"\n📊 *THỐNG KÊ:*\n"
        msg += f"• {balance_data['income_count']} khoản thu\n"
        msg += f"• {balance_data['expense_count']} khoản chi\n"
        
        # Tổng số dư theo từng loại tiền (không quy đổi)
        msg += f"\n💎 *SỐ DƯ HIỆN TẠI:*\n"
        for b in balance_data['balances']:
            if b['balance'] != 0:
                icon = currency_icons.get(b['currency'], '💱')
                msg += f"• {icon} {format_currency_simple(b['balance'], b['currency'])}\n"
        
        msg += f"\n🕐 {format_vn_time()}"
        
        return msg

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
        """Xóa danh mục và tất cả chi tiêu liên quan"""
        logger.info(f"🔍 delete_category được gọi với category_id={category_id}, owner_id={owner_id}")
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # BẬT KHÓA NGOẠI
            c.execute("PRAGMA foreign_keys = ON")
            logger.info(f"🔧 PRAGMA foreign_keys = ON cho user {owner_id}")
            
            # Kiểm tra danh mục có tồn tại không
            logger.info(f"🔍 Đang tìm danh mục ID {category_id} cho user {owner_id}")
            c.execute('''SELECT id, name FROM expense_categories WHERE id = ? AND user_id = ?''', (category_id, owner_id))
            category = c.fetchone()
            
            if not category:
                logger.warning(f"❌ Không tìm thấy danh mục ID {category_id} cho user {owner_id}")
                return False, "❌ Không tìm thấy danh mục!", 0
            
            category_name = category[1]
            logger.info(f"📝 Tìm thấy danh mục: '{category_name}' (ID: {category_id})")
            
            # Đếm số khoản chi
            logger.info(f"🔍 Đếm số khoản chi trong danh mục {category_id}")
            c.execute('''SELECT COUNT(*) FROM expenses WHERE category_id = ? AND user_id = ?''', (category_id, owner_id))
            expenses_count = c.fetchone()[0]
            logger.info(f"📊 Có {expenses_count} khoản chi trong danh mục '{category_name}'")
            
            # Bắt đầu transaction
            logger.info("🔄 Bắt đầu transaction...")
            c.execute("BEGIN TRANSACTION")
            
            # Xóa chi tiêu trước
            logger.info(f"🗑 Đang xóa chi tiêu trong danh mục {category_id}...")
            c.execute('''DELETE FROM expenses WHERE category_id = ? AND user_id = ?''', (category_id, owner_id))
            deleted_expenses = c.rowcount
            logger.info(f"✅ Đã xóa {deleted_expenses} khoản chi (dự kiến: {expenses_count})")
            
            # Xóa danh mục
            logger.info(f"🗑 Đang xóa danh mục {category_id}...")
            c.execute('''DELETE FROM expense_categories WHERE id = ? AND user_id = ?''', (category_id, owner_id))
            
            if c.rowcount == 0:
                logger.error(f"❌ Không thể xóa danh mục {category_id} - rowcount = 0")
                conn.rollback()
                logger.info("↩️ Đã rollback transaction")
                return False, "❌ Không thể xóa danh mục!", 0
            
            # Commit transaction
            conn.commit()
            logger.info("💾 Đã commit transaction")
            
            logger.info(f"✅ ĐÃ XÓA THÀNH CÔNG danh mục '{category_name}' (ID: {category_id}), kèm {deleted_expenses} khoản chi")
            
            return True, category_name, deleted_expenses
            
        except sqlite3.IntegrityError as e:
            # Lỗi ràng buộc khóa ngoại
            if conn:
                conn.rollback()
                logger.info("↩️ Đã rollback transaction do lỗi IntegrityError")
            
            logger.error(f"❌ LỖI INTEGRITY: {e}", exc_info=True)
            logger.error(f"   • category_id: {category_id}")
            logger.error(f"   • owner_id: {owner_id}")
            
            # Thử cách khác: xóa từng bước
            try:
                logger.info("🔄 Thử xóa bằng cách 2 (không dùng transaction)...")
                conn2 = sqlite3.connect(DB_PATH)
                c2 = conn2.cursor()
                
                # Xóa chi tiêu trước
                c2.execute('''DELETE FROM expenses WHERE category_id = ? AND user_id = ?''', (category_id, owner_id))
                deleted = c2.rowcount
                logger.info(f"✅ Cách 2: Đã xóa {deleted} khoản chi")
                
                # Xóa danh mục sau
                c2.execute('''DELETE FROM expense_categories WHERE id = ? AND user_id = ?''', (category_id, owner_id))
                logger.info(f"✅ Cách 2: Đã xóa danh mục")
                
                conn2.commit()
                conn2.close()
                
                logger.info(f"✅ Cách 2 THÀNH CÔNG: đã xóa danh mục '{category_name}', kèm {deleted} khoản chi")
                return True, category_name, deleted
                
            except Exception as e2:
                logger.error(f"❌ Cách 2 cũng thất bại: {e2}", exc_info=True)
                return False, f"❌ Lỗi ràng buộc dữ liệu: {str(e)}", 0
                
        except Exception as e:
            if conn:
                conn.rollback()
                logger.info("↩️ Đã rollback transaction do lỗi Exception")
            
            logger.error(f"❌ LỖI NGOẠI LỆ: {e}", exc_info=True)
            logger.error(f"   • category_id: {category_id}")
            logger.error(f"   • owner_id: {owner_id}")
            logger.error(f"   • Kiểu lỗi: {type(e).__name__}")
            
            return False, str(e), 0
            
        finally:
            if conn:
                conn.close()
                logger.info("🔚 Đã đóng kết nối database")

    def edit_income(income_id, user_id, amount=None, source=None, note=None, currency=None):
        """Sửa thông tin khoản thu
        
        Args:
            income_id: ID khoản thu
            user_id: ID người dùng
            amount: Số tiền mới (None nếu không sửa)
            source: Nguồn thu mới (None nếu không sửa)
            note: Ghi chú mới (None nếu không sửa)
            currency: Loại tiền mới (None nếu không sửa)
        
        Returns:
            (success, message)
        """
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Kiểm tra khoản thu có tồn tại không
            c.execute('''SELECT id FROM incomes WHERE id = ? AND user_id = ?''', (income_id, user_id))
            if not c.fetchone():
                return False, "❌ Không tìm thấy khoản thu!"
            
            # Xây dựng câu lệnh UPDATE dựa trên các trường được cung cấp
            updates = []
            params = []
            
            if amount is not None:
                updates.append("amount = ?")
                params.append(amount)
            
            if source is not None:
                updates.append("source = ?")
                params.append(source)
            
            if note is not None:
                updates.append("note = ?")
                params.append(note)
            
            if currency is not None:
                updates.append("currency = ?")
                params.append(currency)
            
            if not updates:
                return False, "❌ Không có thông tin nào để cập nhật!"
            
            # Thêm điều kiện WHERE
            query = f"UPDATE incomes SET {', '.join(updates)} WHERE id = ? AND user_id = ?"
            params.extend([income_id, user_id])
            
            c.execute(query, params)
            conn.commit()
            
            if c.rowcount > 0:
                logger.info(f"✅ Edited income {income_id} for user {user_id}")
                return True, "✅ Đã cập nhật khoản thu thành công!"
            else:
                return False, "❌ Không thể cập nhật khoản thu!"
                
        except Exception as e:
            logger.error(f"❌ Lỗi edit income: {e}")
            return False, f"❌ Lỗi: {str(e)}"
        finally:
            if conn:
                conn.close()

    def edit_expense(expense_id, user_id, amount=None, category_id=None, note=None, currency=None):
        """Sửa thông tin khoản chi
        
        Args:
            expense_id: ID khoản chi
            user_id: ID người dùng
            amount: Số tiền mới (None nếu không sửa)
            category_id: ID danh mục mới (None nếu không sửa)
            note: Ghi chú mới (None nếu không sửa)
            currency: Loại tiền mới (None nếu không sửa)
        
        Returns:
            (success, message)
        """
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Kiểm tra khoản chi có tồn tại không
            c.execute('''SELECT id FROM expenses WHERE id = ? AND user_id = ?''', (expense_id, user_id))
            if not c.fetchone():
                return False, "❌ Không tìm thấy khoản chi!"
            
            # Nếu có category_id mới, kiểm tra category có tồn tại không
            if category_id is not None:
                c.execute('''SELECT id FROM expense_categories WHERE id = ? AND user_id = ?''', (category_id, user_id))
                if not c.fetchone():
                    return False, "❌ Không tìm thấy danh mục mới!"
            
            # Xây dựng câu lệnh UPDATE
            updates = []
            params = []
            
            if amount is not None:
                updates.append("amount = ?")
                params.append(amount)
            
            if category_id is not None:
                updates.append("category_id = ?")
                params.append(category_id)
            
            if note is not None:
                updates.append("note = ?")
                params.append(note)
            
            if currency is not None:
                updates.append("currency = ?")
                params.append(currency)
            
            if not updates:
                return False, "❌ Không có thông tin nào để cập nhật!"
            
            # Thêm điều kiện WHERE
            query = f"UPDATE expenses SET {', '.join(updates)} WHERE id = ? AND user_id = ?"
            params.extend([expense_id, user_id])
            
            c.execute(query, params)
            conn.commit()
            
            if c.rowcount > 0:
                logger.info(f"✅ Edited expense {expense_id} for user {user_id}")
                return True, "✅ Đã cập nhật khoản chi thành công!"
            else:
                return False, "❌ Không thể cập nhật khoản chi!"
                
        except Exception as e:
            logger.error(f"❌ Lỗi edit expense: {e}")
            return False, f"❌ Lỗi: {str(e)}"
        finally:
            if conn:
                conn.close()

    # ==================== KEYBOARD ====================
    def get_main_keyboard():
        keyboard = [
            [KeyboardButton("💰 ĐẦU TƯ COIN"), KeyboardButton("💵 QUẢN LÝ CHI TIÊU")],
            [KeyboardButton("🤔 HƯỚNG DẪN")]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_invest_menu_keyboard(user_id=None, group_id=None, chat_type=None):
        keyboard = [
            [InlineKeyboardButton("₿ BTC", callback_data="price_BTC"),
             InlineKeyboardButton("Ξ ETH", callback_data="price_ETH"),
             InlineKeyboardButton("Ξ SOL", callback_data="price_SOL"),
             InlineKeyboardButton("💵 USDT", callback_data="price_USDT")],
            [InlineKeyboardButton("📊 Top 10", callback_data="show_top10"),
             InlineKeyboardButton("📈 Lợi nhuận", callback_data="show_profit")],
            [InlineKeyboardButton("✏️ Sửa/Xóa", callback_data="edit_transactions"),
             InlineKeyboardButton("📊 Thống kê", callback_data="show_stats")],
            [InlineKeyboardButton("🔔 Cảnh báo giá", callback_data="show_alerts"),
             InlineKeyboardButton("📥 Xuất CSV", callback_data="export_csv")],
            [InlineKeyboardButton("➕ Mua coin", callback_data="show_buy"),
             InlineKeyboardButton("➖ Bán coin", callback_data="show_sell")]
        ]
        
        # Chỉ hiển thị nút ADMIN nếu đang ở trong group và có quyền
        if group_id and user_id:
            try:
                if chat_type in ['group', 'supergroup'] and check_permission(group_id, user_id, 'view'):
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
    @require_permission('edit')
    async def edit_income_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Sửa khoản thu: /editthu [id] [số tiền] [nguồn] [ghi chú]"""
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        if len(ctx.args) < 2:
            # Hiển thị danh sách thu gần đây để chọn
            recent = get_recent_incomes(owner_id, 10)
            if not recent:
                await update.message.reply_text("📭 Không có khoản thu nào để sửa!")
                return
            
            msg = "✏️ *CHỌN KHOẢN THU CẦN SỬA*\n━━━━━━━━━━━━━━━━\n\n"
            for inc in recent:
                inc_id, amount, source, note, date, currency = inc
                msg += f"• #{inc_id} {date}: {format_currency_simple(amount, currency)} - {source}\n"
                if note:
                    msg += f"  📝 {note}\n"
            
            msg += f"\n🕐 {format_vn_time_short()}\n\n"
            msg += "👉 Dùng: `/editthu [id] [số tiền] [nguồn] [ghi chú]`\n"
            msg += "VD: `/editthu 5 200000 Lương tháng 3`"
            
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        try:
            income_id = int(ctx.args[0])
            
            # Parse các tham số
            amount = None
            source = None
            note = None
            currency = None
            
            if len(ctx.args) >= 2:
                # Thử parse số tiền
                try:
                    amount = float(ctx.args[1].replace(',', ''))
                except:
                    pass
            
            if len(ctx.args) >= 3:
                # Kiểm tra nếu là currency
                if ctx.args[2].upper() in SUPPORTED_CURRENCIES:
                    currency = ctx.args[2].upper()
                    if len(ctx.args) >= 4:
                        source = ctx.args[3]
                        note = " ".join(ctx.args[4:]) if len(ctx.args) > 4 else ""
                else:
                    source = ctx.args[2]
                    note = " ".join(ctx.args[3:]) if len(ctx.args) > 3 else ""
            
            success, message = edit_income(income_id, owner_id, amount, source, note, currency)
            
            if success:
                # Lấy thông tin mới để hiển thị
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT amount, source, note, currency FROM incomes WHERE id = ?''', (income_id,))
                updated = c.fetchone()
                conn.close()
                
                if updated:
                    new_amount, new_source, new_note, new_currency = updated
                    msg = (f"✅ *ĐÃ SỬA KHOẢN THU #{income_id}*\n━━━━━━━━━━━━━━━━\n\n"
                           f"💰 Số tiền: {format_currency_simple(new_amount, new_currency)}\n"
                           f"📌 Nguồn: {new_source}\n"
                           f"📝 Ghi chú: {new_note if new_note else 'Không có'}\n\n"
                           f"🕐 {format_vn_time()}")
                    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(message)
                
        except ValueError:
            await update.message.reply_text("❌ ID không hợp lệ!")
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: {str(e)}")

    @auto_update_user
    @require_permission('edit')
    async def edit_expense_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Sửa khoản chi: /editchi [id] [số tiền] [mã DM] [ghi chú]"""
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        if len(ctx.args) < 2:
            # Hiển thị danh sách chi gần đây để chọn
            recent = get_recent_expenses(owner_id, 10)
            if not recent:
                await update.message.reply_text("📭 Không có khoản chi nào để sửa!")
                return
            
            msg = "✏️ *CHỌN KHOẢN CHI CẦN SỬA*\n━━━━━━━━━━━━━━━━\n\n"
            for exp in recent:
                exp_id, cat_name, amount, note, date, currency = exp
                # Escape các ký tự đặc biệt
                safe_cat = escape_markdown(cat_name)
                safe_note = escape_markdown(note) if note else ""
                msg += f"• #{exp_id} {date}: {format_currency_simple(amount, currency)} - {safe_cat}\n"
                if safe_note:
                    msg += f"  📝 {safe_note}\n"
            
            msg += f"\n🕐 {format_vn_time_short()}\n\n"
            msg += "👉 Dùng: `/editchi [id] [số tiền] [mã DM] [ghi chú]`\n"
            msg += "VD: `/editchi 3 75000 1 Ăn trưa`"
            
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        try:
            expense_id = int(ctx.args[0])
            
            # Parse các tham số
            amount = None
            category_id = None
            note = None
            currency = None
            
            if len(ctx.args) >= 2:
                try:
                    amount = float(ctx.args[1].replace(',', ''))
                except:
                    pass
            
            if len(ctx.args) >= 3:
                # Thử parse category_id
                try:
                    category_id = int(ctx.args[2])
                    if len(ctx.args) >= 4:
                        # Kiểm tra currency
                        if ctx.args[3].upper() in SUPPORTED_CURRENCIES:
                            currency = ctx.args[3].upper()
                            note = " ".join(ctx.args[4:]) if len(ctx.args) > 4 else ""
                        else:
                            note = " ".join(ctx.args[3:]) if len(ctx.args) > 3 else ""
                except:
                    # Nếu không phải số, có thể là note
                    note = " ".join(ctx.args[2:])
            
            success, message = edit_expense(expense_id, owner_id, amount, category_id, note, currency)
            
            if success:
                # Lấy thông tin mới để hiển thị
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT e.amount, ec.name, e.note, e.currency 
                           FROM expenses e 
                           JOIN expense_categories ec ON e.category_id = ec.id 
                           WHERE e.id = ?''', (expense_id,))
                updated = c.fetchone()
                conn.close()
                
                if updated:
                    new_amount, new_cat, new_note, new_currency = updated
                    # Escape các ký tự đặc biệt
                    safe_cat = escape_markdown(new_cat)
                    safe_note = escape_markdown(new_note) if new_note else ""
                    
                    msg = (f"✅ *ĐÃ SỬA KHOẢN CHI #{expense_id}*\n━━━━━━━━━━━━━━━━\n\n"
                           f"💰 Số tiền: {format_currency_simple(new_amount, new_currency)}\n"
                           f"📂 Danh mục: {safe_cat}\n"
                           f"📝 Ghi chú: {safe_note if safe_note else 'Không có'}\n\n"
                           f"🕐 {format_vn_time()}")
                    
                    # Escape toàn bộ msg trước khi gửi
                    safe_msg = escape_markdown(msg)
                    await update.message.reply_text(safe_msg, parse_mode=ParseMode.MARKDOWN)
            else:
                # Message từ edit_expense đã có thể có ký tự đặc biệt
                safe_message = escape_markdown(message)
                await update.message.reply_text(safe_message)
                
        except ValueError:
            await update.message.reply_text("❌ ID không hợp lệ!")
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: {str(e)}")

    @auto_update_user
    async def grant_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Cấp quyền sử dụng bot cho user: /grant @user [quyền]"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        # Kiểm tra có phải trong group không
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        # Chỉ chủ sở hữu nhóm mới được cấp quyền
        owner_id = get_group_owner(chat_id)
        if user_id != owner_id and not is_owner(user_id):
            await update.message.reply_text("❌ Chỉ chủ sở hữu nhóm mới có thể cấp quyền!")
            return
        
        # Nếu không có tham số, hiển thị hướng dẫn
        if not ctx.args:
            # Lấy danh sách user đã có quyền
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                SELECT p.user_id, p.can_view_all, p.can_edit_all, p.can_delete_all, p.can_manage_perms, 
                       u.username, u.first_name 
                FROM permissions p 
                LEFT JOIN users u ON p.user_id = u.user_id 
                WHERE p.group_id = ?
                ORDER BY p.created_at
            ''', (chat_id,))
            granted_users = c.fetchall()
            conn.close()
            
            msg = "📝 *HƯỚNG DẪN CẤP QUYỀN*\n━━━━━━━━━━━━━━━━\n\n"
            msg += "*Các mức quyền:*\n"
            msg += "• `view` - Xem giá, portfolio, lợi nhuận\n"
            msg += "• `edit` - Được thêm/sửa giao dịch\n"
            msg += "• `delete` - Được xóa giao dịch\n"
            msg += "• `manage` - Quản lý phân quyền\n"
            msg += "• `full` - Tất cả quyền trên\n\n"
            
            msg += "*Cú pháp:*\n"
            msg += "`/grant @username view`\n"
            msg += "`/grant @username edit`\n"
            msg += "`/grant @username full`\n\n"
            
            if granted_users:
                msg += "*Danh sách đã cấp quyền:*\n"
                for u in granted_users:
                    uid, view, edit, delete, manage, username, first_name = u
                    display = f"@{username}" if username else first_name or f"User {uid}"
                    perms = []
                    if view: perms.append("👁")
                    if edit: perms.append("✏️")
                    if delete: perms.append("🗑")
                    if manage: perms.append("🔐")
                    msg += f"• {display}: {' '.join(perms)}\n"
            else:
                msg += "*Chưa có ai được cấp quyền*"
            
            msg += f"\n\n🕐 {format_vn_time_short()}"
            
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        # Xử lý cấp quyền
        target = ctx.args[0]
        perm_type = ctx.args[1].lower() if len(ctx.args) > 1 else 'view'
        
        # Lấy user_id từ username
        if target.startswith('@'):
            username = target[1:]
            target_id = get_user_id_by_username(username)
            if not target_id:
                await update.message.reply_text(f"❌ Không tìm thấy user {target}\n\nHãy yêu cầu họ nhắn tin riêng cho bot trước!")
                return
        else:
            try:
                target_id = int(target)
            except:
                await update.message.reply_text("❌ ID không hợp lệ!")
                return
        
        # Không cho tự cấp quyền cho chính mình
        if target_id == user_id:
            await update.message.reply_text("❌ Bạn không thể tự cấp quyền cho chính mình!")
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
            await update.message.reply_text("❌ Loại quyền không hợp lệ! Chỉ chấp nhận: view, edit, delete, manage, full")
            return
        
        # Cấp quyền
        if grant_permission(chat_id, target_id, user_id, permissions):
            # Lấy tên hiển thị
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_id,))
            user_info = c.fetchone()
            conn.close()
            
            display_name = f"@{user_info[0]}" if user_info and user_info[0] else (user_info[1] if user_info else f"User {target_id}")
            
            # Tạo message thông báo
            perm_emoji = {
                'view': '👁',
                'edit': '✏️',
                'delete': '🗑',
                'manage': '🔐',
                'full': '👑'
            }
            
            msg = (f"✅ *CẤP QUYỀN THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n"
                   f"• Người dùng: {display_name}\n"
                   f"• Quyền: {perm_emoji.get(perm_type, '📌')} {perm_type.upper()}\n\n")
            
            if perm_type == 'view':
                msg += "Họ có thể:\n• Xem giá coin\n• Xem portfolio\n• Xem lợi nhuận\n• Xem thống kê"
            elif perm_type == 'edit':
                msg += "Họ có thể:\n• Xem dữ liệu\n• Thêm giao dịch mới\n• Sửa giao dịch"
            elif perm_type == 'delete':
                msg += "Họ có thể:\n• Xem dữ liệu\n• Xóa giao dịch"
            elif perm_type == 'manage':
                msg += "Họ có thể:\n• Quản lý phân quyền\n• Cấp/thu hồi quyền cho người khác"
            elif perm_type == 'full':
                msg += "Họ có TOÀN QUYỀN trong nhóm này!"
            
            msg += f"\n\n🕐 {format_vn_time()}"
            
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            
            # Thông báo cho người được cấp quyền
            try:
                await ctx.bot.send_message(
                    target_id,
                    f"✅ *BẠN ĐÃ ĐƯỢC CẤP QUYỀN*\n━━━━━━━━━━━━━━━━\n\n"
                    f"• Nhóm: {update.effective_chat.title}\n"
                    f"• Quyền: {perm_emoji.get(perm_type, '📌')} {perm_type.upper()}\n\n"
                    f"Bạn có thể sử dụng bot trong nhóm này!\n\n"
                    f"🕐 {format_vn_time()}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
        else:
            await update.message.reply_text("❌ Lỗi khi cấp quyền! Vui lòng thử lại sau.")

    @auto_update_user
    async def myperm_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Kiểm tra quyền của bản thân trong nhóm: /myperm"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        # Trong private chat
        if chat_type == 'private':
            msg = (
                f"👤 *THÔNG TIN CÁ NHÂN*\n━━━━━━━━━━━━━━━━\n\n"
                f"• ID: `{user_id}`\n"
                f"• Username: @{update.effective_user.username or 'None'}\n\n"
                f"📌 Trong private chat, bạn có toàn quyền với dữ liệu của mình.\n\n"
                f"🕐 {format_vn_time()}"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        # Trong group
        if chat_type in ['group', 'supergroup']:
            # Lấy tên nhóm từ Telegram
            try:
                chat = await ctx.bot.get_chat(chat_id)
                group_name = chat.title or "Nhóm không tên"
            except:
                group_name = "Nhóm này"
            
            # Kiểm tra có phải chủ sở hữu không
            owner_id = get_group_owner(chat_id)
            is_group_owner = (user_id == owner_id)
            
            # Lấy thông tin quyền từ database
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT can_view_all, can_edit_all, can_delete_all, can_manage_perms 
                        FROM permissions WHERE group_id = ? AND user_id = ?''', (chat_id, user_id))
            result = c.fetchone()
            conn.close()
            
            # Tạo message
            msg = f"🔐 *QUYỀN CỦA BẠN TRONG NHÓM*\n━━━━━━━━━━━━━━━━\n\n"
            msg += f"📌 Nhóm: {group_name}\n"
            msg += f"👤 Bạn: @{update.effective_user.username or 'None'} (`{user_id}`)\n\n"
            
            if is_group_owner:
                msg += "👑 *BẠN LÀ CHỦ SỞ HỮU NHÓM*\n• Có TOÀN QUYỀN quản lý dữ liệu\n• Có thể cấp quyền cho người khác\n\n"
            elif is_owner(user_id):
                msg += "👑 *BẠN LÀ OWNER BOT*\n• Có TOÀN QUYỀN ở mọi nhóm\n\n"
            elif result:
                can_view, can_edit, can_delete, can_manage = result
                msg += "*CHI TIẾT QUYỀN:*\n"
                msg += f"• 👁 Xem dữ liệu: {'✅' if can_view else '❌'}\n"
                msg += f"• ✏️ Thêm/sửa giao dịch: {'✅' if can_edit else '❌'}\n"
                msg += f"• 🗑 Xóa giao dịch: {'✅' if can_delete else '❌'}\n"
                msg += f"• 🔐 Quản lý phân quyền: {'✅' if can_manage else '❌'}\n"
                
                if can_edit or can_delete or can_manage:
                    msg += "\n📌 Bạn có thể quản lý dữ liệu của chủ sở hữu nhóm.\n"
            else:
                msg += "❌ *BẠN CHƯA CÓ QUYỀN*\n\n"
                msg += "Bạn chưa được cấp quyền sử dụng bot trong nhóm này.\n"
                msg += "Vui lòng liên hệ chủ sở hữu nhóm để được cấp quyền.\n"
            
            msg += f"\n🕐 {format_vn_time()}"
            
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
            
    @auto_update_user
    async def whoami_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''SELECT user_id, username, first_name, last_name, last_seen FROM users WHERE user_id = ?''', (user.id,))
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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền quản lý phân quyền!")
            return
        
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ Hãy reply tin nhắn của người cần grant!")
            return
        
        if not ctx.args:
            await update.message.reply_text("❌ Thiếu loại quyền! VD: `/permgrant view`", parse_mode=ParseMode.MARKDOWN)
            return
        
        target_user = update.message.reply_to_message.from_user
        perm_type = ctx.args[0].lower()
        
        await update_user_info_async(target_user)
        
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
            await update.message.reply_text(f"✅ Đã cấp quyền {perm_type} cho @{target_user.username or target_user.id}")
        else:
            await update.message.reply_text("❌ Lỗi khi cấp quyền!")

    @auto_update_user
    async def getid_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền!")
            return
        
        msg = await update.message.reply_text("🔄 Đang đồng bộ danh sách thành viên...")
        
        try:
            admins = await ctx.bot.get_chat_administrators(chat_id)
            count = 0
            
            for admin in admins:
                if admin.user:
                    await update_user_info_async(admin.user)
                    count += 1
            
            await msg.edit_text(f"✅ *ĐỒNG BỘ THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n📊 Đã cập nhật: {count} admin\n👥 Tổng số: {len(admins)} thành viên\n\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await msg.edit_text(f"❌ Lỗi: {e}")

    @auto_update_user
    async def owner_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not is_owner(user_id):
            await update.message.reply_text("❌ Chỉ Owner mới có quyền sử dụng lệnh này!")
            return
        
        if not ctx.args:
            msg = ("👑 *OWNER PANEL*\n━━━━━━━━━━━━━━━━\n\n"
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
                   f"🕐 {format_vn_time()}")
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
                await update.message.reply_text(f"✅ Đã thêm @{target} làm nhân viên!\nHọ có thể quản lý dữ liệu trong group này.")
            else:
                await update.message.reply_text("❌ Lỗi khi thêm nhân viên!")

        elif action == "removestaff" and len(ctx.args) >= 2:
            target = ctx.args[1]
            target_id = await resolve_user_id(target, ctx)
            
            if not target_id:
                await update.message.reply_text("❌ Không tìm thấy user!")
                return
            
            chat_id = update.effective_chat.id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT role FROM permissions WHERE group_id = ? AND user_id = ?''', (chat_id, target_id))
            result = c.fetchone()
            
            if not result or result[0] != 'staff':
                conn.close()
                await update.message.reply_text(f"❌ {target} không phải là nhân viên!")
                return
            conn.close()
            
            if revoke_permission(chat_id, target_id):
                await update.message.reply_text(f"✅ Đã xóa @{target} khỏi danh sách nhân viên!")
            else:
                await update.message.reply_text("❌ Lỗi khi xóa nhân viên!")

        elif action == "liststaff":
            chat_id = update.effective_chat.id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT p.user_id, p.can_view_all, p.can_edit_all, p.can_delete_all, p.can_manage_perms, u.username, u.first_name FROM permissions p LEFT JOIN users u ON p.user_id = u.user_id WHERE p.group_id = ? AND p.role = 'staff' ORDER BY p.created_at''', (chat_id,))
            staff_list = c.fetchall()
            conn.close()
            
            if not staff_list:
                await update.message.reply_text("📭 Chưa có nhân viên nào!")
                return
            
            msg = "👥 *DANH SÁCH NHÂN VIÊN*\n━━━━━━━━━━━━━━━━\n\n"
            for staff in staff_list:
                user_id, view, edit, delete, manage, username, first_name = staff
                
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
            
            msg += f"\n🕐 {format_vn_time()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

        elif action == "revoke" and len(ctx.args) >= 2:
            target = ctx.args[1]
            target_id = await resolve_user_id(target, ctx)
            
            if not target_id:
                await update.message.reply_text("❌ Không tìm thấy user!")
                return
            
            chat_id = update.effective_chat.id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT role FROM permissions WHERE group_id = ? AND user_id = ?''', (chat_id, target_id))
            result = c.fetchone()
            conn.close()
            
            if not result:
                await update.message.reply_text(f"❌ {target} chưa được cấp quyền!")
                return
            
            if target_id == user_id:
                await update.message.reply_text("❌ Không thể tự thu hồi quyền của chính mình!")
                return
            
            if revoke_permission(chat_id, target_id):
                await update.message.reply_text(f"✅ Đã thu hồi toàn bộ quyền của {target}!")
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
                await update.message.reply_text(f"✅ Đã duyệt @{target} sử dụng bot!\nHọ có thể xem dữ liệu trong group này.")
            else:
                await update.message.reply_text("❌ Lỗi khi duyệt user!")
        
        elif action == "listpending":
            chat_id = update.effective_chat.id
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT user_id, username, first_name, created_at FROM permissions WHERE group_id = ? AND is_approved = 0 AND role = 'user' ORDER BY created_at''', (chat_id,))
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
            
            msg = ("📊 *THỐNG KÊ HỆ THỐNG*\n━━━━━━━━━━━━━━━━\n\n"
                   f"👥 *Tổng user:* {total_users}\n"
                   f"👑 *Nhân viên:* {total_staff}\n"
                   f"✅ *Đã duyệt:* {total_approved}\n"
                   f"⏳ *Chờ duyệt:* {total_pending}\n\n"
                   f"💼 *Giao dịch:* {total_transactions}\n"
                   f"👤 *User có portfolio:* {users_with_portfolio}\n\n"
                   f"🕐 {format_vn_time()}")
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    async def resolve_user_id(target, ctx):
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
    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type in ['group', 'supergroup']:
            welcome_msg = ("🚀 *ĐẦU TƯ COIN & QUẢN LÝ CHI TIÊU*\n\n"
                           "🤖 Bot đã sẵn sàng!\n\n"
                           "*Các lệnh trong nhóm:*\n"
                           "• `/s btc eth` - Xem giá coin\n"
                           "• `/usdt` - Tỷ giá USDT/VND\n"
                           "• `/buy btc 0.5 40000` - Mua coin\n"
                           "• `/sell btc 0.2` - Bán coin\n\n"
                           "📱 *Vuốt xuống để hiện menu*\n"
                           f"🕐 {format_vn_time()}")
            await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())
        else:
            welcome_msg = ("🚀 *ĐẦU TƯ COIN & QUẢN LÝ CHI TIÊU*\n\n"
                           "🤖 Bot hỗ trợ:\n\n"
                           "*💎 ĐẦU TƯ COIN:*\n"
                           "• Xem giá coin\n• Top 10 coin\n• Quản lý danh mục\n• Tính lợi nhuận\n• Cảnh báo giá\n\n"
                           "*💰 QUẢN LÝ CHI TIÊU:*\n"
                           "• Ghi chép thu/chi\n• Đa tiền tệ\n• Quản lý ngân sách\n• Báo cáo ngày/tháng/năm\n\n"
                           f"🕐 *Hiện tại:* `{format_vn_time()}`\n\n"
                           "👇 *Chọn chức năng bên dưới*")
            await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())

    @auto_update_user
    async def menu_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("👇 *Chọn chức năng bên dưới*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())

    @auto_update_user
    async def hide_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Đã ẩn bàn phím. Gõ /menu để hiện lại.", reply_markup=ReplyKeyboardRemove())

    @auto_update_user
    async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        help_msg = "📘 *HƯỚNG DẪN SỬ DỤNG BOT*\n━━━━━━━━━━━━━━━━\n\n"
        
        help_msg += (
            "*💰 ĐẦU TƯ COIN:*\n"
            "• `/s btc eth` - Xem giá coin\n"
            "• `/usdt` - Tỷ giá USDT/VND\n"
            "• `/buy btc 0.5 40000` - Mua coin\n"
            "• `/sell btc 0.2` - Bán coin\n"
            "• `/alert BTC above 50000` - Cảnh báo giá\n\n"
            
            "*💸 QUẢN LÝ CHI TIÊU:*\n"
            "• `tn 500000` - Thêm thu nhập\n"
            "• `dm Ăn uống 3000000` - Tạo danh mục\n"
            "• `ct 1 50000` - Thêm chi tiêu\n"
            "• `ds` - Xem giao dịch gần đây\n"
            "• `/balance` - Xem cân đối thu chi\n\n"
            
            "*🔐 QUẢN LÝ NHÓM:*\n"
            "• `/grant @user view` - Cấp quyền xem\n"
            "• `/myperm` - Kiểm tra quyền của bạn\n"
            "• `/groupinfo` - Thông tin nhóm\n\n"
            
            f"🕐 {format_vn_time()}"
        )
        
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
    @require_permission('view')
    async def usdt_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("🔄 Đang tra cứu...")
        rate_data = get_usdt_vnd_rate()
        vnd = rate_data['vnd']
        
        text = ("💱 *TỶ GIÁ USDT/VND*\n━━━━━━━━━━━━━━━━\n\n"
                f"🇺🇸 *1 USDT* = `{fmt_vnd(vnd)}`\n"
                f"🇻🇳 *1,000,000 VND* = `{1000000/vnd:.4f} USDT`\n\n"
                f"⏱ *Cập nhật:* `{rate_data['update_time']}`\n"
                f"📊 *Nguồn:* `{rate_data['source']}`")
        
        keyboard = [[InlineKeyboardButton("🔄 Làm mới", callback_data="refresh_usdt")],
                    [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
        
        await msg.delete()
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    @auto_update_user
    @require_permission('view')
    async def s_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await update.message.reply_text("❌ /s btc eth doge")
        
        msg = await update.message.reply_text("🔄 Đang tra cứu...")
        
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
    @require_permission('edit')
    async def buy_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Xác định user_id thực sự cần thêm giao dịch
        chat_type = update.effective_chat.type
        current_user_id = update.effective_user.id
        
        if chat_type == 'private':
            # Private chat: thêm cho chính mình
            target_user_id = current_user_id
            logger.info(f"💬 PRIVATE: mua coin cho user {target_user_id}")
        else:
            # Group chat: thêm cho chủ sở hữu (nếu có quyền)
            target_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
            logger.info(f"👥 GROUP: mua coin cho owner {target_user_id}")
        
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
        
        if add_transaction(target_user_id, symbol, amount, buy_price):
            current_price = price_data['p']
            profit = (current_price - buy_price) * amount
            profit_percent = ((current_price - buy_price) / buy_price) * 100
            
            added_by = f" (thêm bởi @{update.effective_user.username})" if update.effective_user.username else ""
            
            # Thông báo ai là người sở hữu
            owner_info = ""
            if chat_type != 'private' and target_user_id != current_user_id:
                owner_info = f"\n📌 Dữ liệu thuộc về chủ sở hữu group"
            
            msg = (f"✅ *ĐÃ MUA {symbol}*{added_by}\n━━━━━━━━━━━━━━━━\n\n"
                   f"📊 SL: `{amount:.4f}`\n"
                   f"💰 Giá mua: `{fmt_price(buy_price)}`\n"
                   f"💵 Vốn: `{fmt_price(amount * buy_price)}`\n"
                   f"📈 Giá hiện: `{fmt_price(current_price)}`\n"
                   f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%){owner_info}\n\n"
                   f"🕐 {format_vn_time()}")
            await update.message.reply_text(msg, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Lỗi khi thêm giao dịch *{symbol}*", parse_mode='Markdown')

    @auto_update_user
    @require_permission('edit')
    async def sell_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Xác định user_id thực sự
        chat_type = update.effective_chat.type
        current_user_id = update.effective_user.id
        
        if chat_type == 'private':
            target_user_id = current_user_id
            logger.info(f"💬 PRIVATE: bán coin cho user {target_user_id}")
        else:
            target_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
            logger.info(f"👥 GROUP: bán coin cho owner {target_user_id}")
        
        if len(ctx.args) < 2:
            return await update.message.reply_text("❌ /sell btc 0.2")
        
        symbol = ctx.args[0].upper()
        
        try:
            sell_amount = float(ctx.args[1])
        except ValueError:
            return await update.message.reply_text("❌ Số lượng không hợp lệ!")
        
        if sell_amount <= 0:
            return await update.message.reply_text("❌ Số lượng phải > 0")
        
        portfolio_data = get_portfolio(target_user_id)
        if not portfolio_data:
            return await update.message.reply_text("📭 Danh mục trống!")
        
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
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM portfolio WHERE user_id = ?", (owner_id,))
        for tx in new_portfolio:
            c.execute('''INSERT INTO portfolio (user_id, symbol, amount, buy_price, buy_date, total_cost) VALUES (?, ?, ?, ?, ?, ?)''',
                      (owner_id, tx['symbol'], tx['amount'], tx['buy_price'], tx['buy_date'], tx['total_cost']))
        conn.commit()
        conn.close()
        
        profit = sold_value - sold_cost
        profit_percent = (profit / sold_cost) * 100 if sold_cost > 0 else 0
        
        sold_by = f" (bán bởi @{update.effective_user.username})" if update.effective_user.username else ""
        
        msg = (f"✅ *ĐÃ BÁN {sell_amount:.4f} {symbol}*{sold_by}\n━━━━━━━━━━━━━━━━\n\n"
               f"💰 Giá bán: `{fmt_price(current_price)}`\n"
               f"💵 Giá trị: `{fmt_price(sold_value)}`\n"
               f"📊 Vốn: `{fmt_price(sold_cost)}`\n"
               f"{'✅' if profit>=0 else '❌'} LN: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n"
               f"🕐 {format_vn_time()}")
        await update.message.reply_text(msg, parse_mode='Markdown')
        
    @auto_update_user
    @require_permission('edit')
    async def edit_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Xác định user_id thực sự cần sửa
        chat_type = update.effective_chat.type
        current_user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if chat_type == 'private':
            # Private chat: sửa dữ liệu của chính mình
            target_user_id = current_user_id
            logger.info(f"💬 PRIVATE: sửa giao dịch cho user {target_user_id}")
        else:
            # Group chat: sửa dữ liệu của chủ sở hữu (nếu có quyền)
            target_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
            logger.info(f"👥 GROUP: sửa giao dịch cho owner {target_user_id}")
        
        logger.info(f"✏️ edit_command: target_user_id={target_user_id}, current_user={current_user_id}")
        
        # Kiểm tra quyền admin trong group
        is_admin = False
        if chat_type in ['group', 'supergroup']:
            is_admin = check_permission(chat_id, current_user_id, 'edit') or \
                       check_permission(chat_id, current_user_id, 'delete') or \
                       check_permission(chat_id, current_user_id, 'manage')
        
        logger.info(f"🔑 is_admin: {is_admin}")
        
        # Nếu không có tham số, hiển thị danh sách giao dịch
        if not ctx.args:
            # Lấy danh sách giao dịch của target_user
            transactions = get_transaction_detail(target_user_id)
            
            if not transactions:
                await update.message.reply_text("📭 Danh mục trống!")
                return
    
            msg = "📝 *CHỌN GIAO DỊCH*\n━━━━━━━━━━━━━━━━\n\n"
            keyboard = []
            row = []
    
            for i, tx in enumerate(transactions, 1):
                tx_id, symbol, amount, price, date, total = tx
                short_date = date.split()[0] if date else "N/A"
                msg += f"*{i}.* #{tx_id}: {symbol} - {amount:.4f} @ {fmt_price(price)} - {short_date}\n"
    
                row.append(InlineKeyboardButton(f"✏️ #{tx_id}", callback_data=f"edit_{tx_id}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
    
            if row:
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")])
    
            msg += f"\n🕐 {format_vn_time_short()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, 
                                          reply_markup=InlineKeyboardMarkup(keyboard))
            return
    
        # Xem chi tiết 1 giao dịch
        if len(ctx.args) == 1:
            try:
                tx_id = int(ctx.args[0])
                
                # Lấy chi tiết giao dịch từ database
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT id, symbol, amount, buy_price, buy_date, total_cost, user_id 
                            FROM portfolio WHERE id = ?''', (tx_id,))
                tx = c.fetchone()
                conn.close()
                
                if not tx:
                    await update.message.reply_text(f"❌ Không tìm thấy giao dịch #{tx_id}")
                    return
                
                tx_id, symbol, amount, price, date, total, tx_owner_id = tx
                
                # Kiểm tra quyền xem
                if tx_owner_id != target_user_id and not is_admin:
                    await update.message.reply_text("❌ Bạn không có quyền xem giao dịch này!")
                    return
                
                price_data = get_price(symbol)
                current_price = price_data['p'] if price_data else 0
                profit = (current_price - price) * amount if current_price else 0
                profit_percent = ((current_price - price) / price) * 100 if price and current_price else 0
                
                msg = (f"📝 *GIAO DỊCH #{tx_id}*\n━━━━━━━━━━━━━━━━\n\n"
                       f"*{symbol}*\n"
                       f"📅 Ngày mua: {date}\n"
                       f"📊 Số lượng: `{amount:.4f}`\n"
                       f"💰 Giá mua: `{fmt_price(price)}`\n"
                       f"💵 Tổng vốn: `{fmt_price(total)}`\n"
                       f"📈 Giá hiện tại: `{fmt_price(current_price)}`\n"
                       f"{'✅' if profit>=0 else '❌'} Lợi nhuận: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n")
                
                # Thêm hướng dẫn sửa/xóa
                if tx_owner_id == target_user_id or is_admin:
                    msg += f"*Sửa:* `/edit {tx_id} [sl] [giá]`\n"
                    msg += f"*Xóa:* `/del {tx_id}`\n\n"
                else:
                    msg += f"*Chỉ xem, không được sửa/xóa*\n\n"
                
                msg += f"🕐 {format_vn_time()}"
                
                # Tạo keyboard
                keyboard = []
                if tx_owner_id == target_user_id or is_admin:
                    keyboard.append([
                        InlineKeyboardButton("✏️ Sửa", callback_data=f"edit_{tx_id}"),
                        InlineKeyboardButton("🗑 Xóa", callback_data=f"del_{tx_id}")
                    ])
                keyboard.append([InlineKeyboardButton("🔙 Về danh sách", callback_data="edit_transactions")])
                
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, 
                                              reply_markup=InlineKeyboardMarkup(keyboard))
                                              
            except ValueError:
                await update.message.reply_text("❌ ID không hợp lệ")
        
        # Sửa giao dịch
        elif len(ctx.args) == 3:
            try:
                tx_id = int(ctx.args[0])
                new_amount = float(ctx.args[1])
                new_price = float(ctx.args[2])
                
                if new_amount <= 0 or new_price <= 0:
                    await update.message.reply_text("❌ SL và giá phải > 0")
                    return
                
                # Kiểm tra giao dịch có tồn tại không
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT user_id FROM portfolio WHERE id = ?''', (tx_id,))
                result = c.fetchone()
                
                if not result:
                    await update.message.reply_text(f"❌ Không tìm thấy giao dịch #{tx_id}")
                    conn.close()
                    return
                
                tx_owner_id = result[0]
                
                # Kiểm tra quyền sửa
                can_edit = False
                if tx_owner_id == target_user_id:
                    can_edit = True
                    logger.info(f"✅ User {current_user_id} là chủ, được sửa #{tx_id}")
                elif is_admin:
                    can_edit = True
                    logger.info(f"✅ Admin {current_user_id} có quyền, được sửa #{tx_id}")
                else:
                    logger.info(f"❌ User {current_user_id} không có quyền sửa #{tx_id}")
                
                if not can_edit:
                    await update.message.reply_text("❌ Bạn không có quyền sửa giao dịch này!")
                    conn.close()
                    return
                
                # Thực hiện sửa
                new_total = new_amount * new_price
                c.execute('''UPDATE portfolio SET amount = ?, buy_price = ?, total_cost = ? 
                            WHERE id = ?''', (new_amount, new_price, new_total, tx_id))
                conn.commit()
                affected = c.rowcount
                conn.close()
                
                if affected > 0:
                    msg = (f"✅ *ĐÃ SỬA GIAO DỊCH #{tx_id}*\n━━━━━━━━━━━━━━━━\n\n"
                           f"📊 SL mới: `{new_amount:.4f}`\n"
                           f"💰 Giá mới: `{fmt_price(new_price)}`\n"
                           f"💵 Vốn mới: `{fmt_price(new_total)}`\n\n"
                           f"🕐 {format_vn_time()}")
                    await update.message.reply_text(msg, parse_mode='Markdown')
                else:
                    await update.message.reply_text(f"❌ Không thể sửa giao dịch #{tx_id}")
                    
            except ValueError:
                await update.message.reply_text("❌ /edit [id] [sl] [giá]")
        else:
            await update.message.reply_text("❌ /edit - Xem DS\n/edit [id] - Xem chi tiết\n/edit [id] [sl] [giá] - Sửa")

    @auto_update_user
    @require_permission('delete')
    async def delete_tx_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Xác định user_id thực sự cần xóa
        chat_type = update.effective_chat.type
        current_user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if chat_type == 'private':
            # Private chat: xóa dữ liệu của chính mình
            target_user_id = current_user_id
            logger.info(f"💬 PRIVATE: xóa giao dịch cho user {target_user_id}")
        else:
            # Group chat: xóa dữ liệu của chủ sở hữu (nếu có quyền)
            target_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
            logger.info(f"👥 GROUP: xóa giao dịch cho owner {target_user_id}")
        
        logger.info(f"🗑 delete_tx_command: target_user_id={target_user_id}, current_user={current_user_id}")
        
        # Kiểm tra quyền admin trong group
        is_admin = False
        if chat_type in ['group', 'supergroup']:
            is_admin = check_permission(chat_id, current_user_id, 'delete') or \
                       check_permission(chat_id, current_user_id, 'manage')
        
        if not ctx.args:
            # Hiển thị danh sách giao dịch để chọn xóa
            transactions = get_transaction_detail(target_user_id)
            
            if not transactions:
                await update.message.reply_text("📭 Danh mục trống!")
                return
    
            msg = "🗑 *CHỌN GIAO DỊCH CẦN XÓA*\n━━━━━━━━━━━━━━━━\n\n"
            keyboard = []
            row = []
    
            for i, tx in enumerate(transactions, 1):
                tx_id, symbol, amount, price, date, total = tx
                short_date = date.split()[0] if date else "N/A"
                msg += f"*{i}.* #{tx_id}: {symbol} - {amount:.4f} @ {fmt_price(price)} - {short_date}\n"
    
                row.append(InlineKeyboardButton(f"🗑 #{tx_id}", callback_data=f"del_{tx_id}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
    
            if row:
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")])
    
            msg += f"\n🕐 {format_vn_time_short()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, 
                                          reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        try:
            tx_id = int(ctx.args[0])
            
            # Kiểm tra giao dịch có tồn tại không
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT user_id FROM portfolio WHERE id = ?''', (tx_id,))
            result = c.fetchone()
            conn.close()
            
            if not result:
                await update.message.reply_text(f"❌ Không tìm thấy giao dịch #{tx_id}")
                return
            
            tx_owner_id = result[0]
            
            # Kiểm tra quyền xóa
            can_delete = False
            
            if tx_owner_id == target_user_id:
                can_delete = True
                logger.info(f"✅ User {current_user_id} là chủ, được xóa #{tx_id}")
            elif is_admin:
                can_delete = True
                logger.info(f"✅ Admin {current_user_id} có quyền delete, được xóa #{tx_id}")
            else:
                logger.info(f"❌ User {current_user_id} không có quyền xóa #{tx_id}")
            
            if not can_delete:
                await update.message.reply_text("❌ Bạn không có quyền xóa giao dịch này!")
                return
            
            # Hỏi xác nhận
            keyboard = [[InlineKeyboardButton("✅ Có", callback_data=f"confirm_del_{tx_id}"),
                         InlineKeyboardButton("❌ Không", callback_data="edit_transactions")]]
            
            await update.message.reply_text(f"⚠️ *Xác nhận xóa giao dịch #{tx_id}?*\n\n🕐 {format_vn_time_short()}", 
                                           parse_mode=ParseMode.MARKDOWN,
                                           reply_markup=InlineKeyboardMarkup(keyboard))
        except ValueError:
            await update.message.reply_text("❌ ID không hợp lệ")
        
    @auto_update_user
    @require_permission('view')
    async def alert_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        
        if add_alert(owner_id, symbol, target_price, condition):
            msg = (f"✅ *ĐÃ TẠO CẢNH BÁO*\n━━━━━━━━━━━━━━━━\n\n"
                   f"• Coin: *{symbol}*\n"
                   f"• Mốc giá: `{fmt_price(target_price)}`\n"
                   f"• Giá hiện tại: `{fmt_price(price_data['p'])}`\n"
                   f"• Điều kiện: {'📈 Lên trên' if condition == 'above' else '📉 Xuống dưới'}\n\n"
                   f"🕐 {format_vn_time()}")
            await update.message.reply_text(msg, parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Lỗi khi tạo cảnh báo!")

    @auto_update_user
    @require_permission('view')
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
    @require_permission('view')
    async def stats_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        msg = await update.message.reply_text("🔄 Đang tính toán thống kê...")
        
        stats = get_portfolio_stats(uid)
        
        if not stats:
            await msg.edit_text("📭 Danh mục trống!")
            return
        
        stats_msg = (f"📊 *THỐNG KÊ DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                     f"*TỔNG QUAN*\n"
                     f"• Vốn: `{fmt_price(stats['total_invest'])}`\n"
                     f"• Giá trị: `{fmt_price(stats['total_value'])}`\n"
                     f"• Lợi nhuận: `{fmt_price(stats['total_profit'])}`\n"
                     f"• Tỷ suất: `{stats['total_profit_percent']:+.2f}%`\n\n"
                     f"*📈 TOP COIN LỜI NHẤT*\n")
        
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

    @auto_update_user
    async def view_portfolio_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not check_permission(chat_id, user_id, 'view'):
            await update.message.reply_text("❌ Bạn không có quyền xem dữ liệu!")
            return
        
        if not ctx.args:
            await update.message.reply_text("❌ /view [@username hoặc ID]")
            return
        
        target = ctx.args[0]
        target_user_id = None
        
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
        
        portfolio_data = get_portfolio(target_user_id)
        
        if not portfolio_data:
            await update.message.reply_text(f"📭 Danh mục của {target} trống!")
            return
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
        user_info = c.fetchone()
        conn.close()
        
        display_name = user_info[0] if user_info and user_info[0] else f"User {target_user_id}"
        
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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not check_permission(chat_id, user_id, 'view'):
            await update.message.reply_text("❌ Bạn không có quyền xem danh sách!")
            return
        
        try:
            admins = await ctx.bot.get_chat_administrators(chat_id)
            
            msg = "👥 *THÀNH VIÊN TRONG NHÓM*\n━━━━━━━━━━━━━━━━\n\n"
            
            for admin in admins:
                user = admin.user
                status = "👑 Admin" if admin.status in ['administrator', 'creator'] else "👤 Member"
                
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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này!")
            return
        
        msg = await update.message.reply_text("🔄 Đang đồng bộ danh sách admin...")
        
        try:
            admins = await ctx.bot.get_chat_administrators(chat_id)
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            granted_count = 0
            updated_count = 0
            
            for admin in admins:
                if admin.user:
                    await update_user_info_async(admin.user)
                    
                    c.execute("SELECT * FROM permissions WHERE group_id = ? AND user_id = ?", (chat_id, admin.user.id))
                    exists = c.fetchone()
                    
                    if not exists:
                        permissions = {'view': 1, 'edit': 0, 'delete': 0, 'manage': 0}
                        role = 'user'
                        
                        if admin.status == 'creator':
                            permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 1}
                            role = 'staff'
                        elif admin.status == 'administrator':
                            permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 0}
                            role = 'staff'
                        
                        created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
                        c.execute('''INSERT INTO permissions (group_id, user_id, granted_by, is_approved, role, can_view_all, can_edit_all, can_delete_all, can_manage_perms, created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (chat_id, admin.user.id, user_id, 1, role,
                                   permissions['view'], permissions['edit'], permissions['delete'], permissions['manage'],
                                   created_at, created_at))
                        granted_count += 1
                    else:
                        updated_count += 1
            
            conn.commit()
            conn.close()
            
            await msg.edit_text(f"✅ *ĐỒNG BỘ ADMIN THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n📊 Kết quả:\n• Tổng số admin trong group: {len(admins)}\n• Đã cấp quyền mới: {granted_count}\n• Đã cập nhật: {updated_count}\n\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await msg.edit_text(f"❌ Lỗi: {e}")

    @auto_update_user
    async def new_chat_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        for new_member in update.message.new_chat_members:
            await update_user_info_async(new_member)
            
            if new_member.is_bot:
                continue
            
            chat_id = update.effective_chat.id
            
            try:
                admins = await ctx.bot.get_chat_administrators(chat_id)
                for admin in admins:
                    if admin.user.id == new_member.id:
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        
                        c.execute("SELECT * FROM permissions WHERE group_id = ? AND user_id = ?", (chat_id, new_member.id))
                        exists = c.fetchone()
                        
                        if not exists:
                            permissions = {'view': 1, 'edit': 0, 'delete': 0, 'manage': 0}
                            
                            if admin.status == 'creator':
                                permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 1}
                            
                            c.execute('''INSERT INTO permissions (group_id, user_id, granted_by, can_view_all, can_edit_all, can_delete_all, can_manage_perms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                                      (chat_id, new_member.id, new_member.id,
                                       permissions['view'], permissions['edit'], permissions['delete'], permissions['manage'],
                                       get_vn_time().strftime("%Y-%m-%d %H:%M:%S")))
                            conn.commit()
                            
                            logger.info(f"✅ Auto-granted permissions for new admin @{new_member.username} in {chat_id}")
                        
                        conn.close()
                        break
            except Exception as e:
                logger.error(f"❌ Lỗi xử lý new member: {e}")

    @auto_update_user
    async def check_perm_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        target_id = user_id
        target_name = "bạn"
        
        if update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
            target_name = f"@{update.message.reply_to_message.from_user.username or target_id}"
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute('''SELECT can_view_all, can_edit_all, can_delete_all, can_manage_perms FROM permissions WHERE group_id = ? AND user_id = ?''', (chat_id, target_id))
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
            admins = await ctx.bot.get_chat_administrators(chat_id)
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            synced = 0
            for admin in admins:
                if admin.user:
                    current_time = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
                    
                    c.execute('''INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, last_seen) VALUES (?, ?, ?, ?, ?)''',
                              (admin.user.id, admin.user.username, admin.user.first_name, admin.user.last_name, current_time))
                    synced += 1
            
            conn.commit()
            conn.close()
            
            username_cache.clear()
            
            await msg.edit_text(f"✅ *ĐỒNG BỘ DỮ LIỆU THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n📊 Đã đồng bộ: {synced} user\n💾 Cache đã được làm mới\n\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await msg.edit_text(f"❌ Lỗi: {e}")

    @auto_update_user
    async def debug_perm_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not is_owner(user_id):
            await update.message.reply_text("❌ Chỉ Owner mới có quyền sử dụng lệnh này!")
            return
        
        chat_id = update.effective_chat.id
        
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='permissions'")
            if not c.fetchone():
                await update.message.reply_text("❌ Bảng permissions chưa được tạo!")
                conn.close()
                return
            
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
            
            c.execute("SELECT * FROM permissions WHERE group_id = ?", (chat_id,))
            rows = c.fetchall()
            
            msg += f"\n*DỮ LIỆU ({len(rows)} rows):*\n"
            if rows:
                for row in rows:
                    msg += f"• `{row}`\n"
            else:
                msg += "• Không có dữ liệu\n"
            
            c.execute("SELECT * FROM permissions WHERE group_id = ? AND user_id = ?", (chat_id, user_id))
            user_perm = c.fetchone()
            
            msg += f"\n*QUYỀN CỦA BẠN:*\n"
            if user_perm:
                msg += f"• {user_perm}\n"
            else:
                msg += "• Chưa có quyền trong group này\n"
            
            conn.close()
            
            if len(msg) > 4000:
                chunks = [msg[i:i+4000] for i in range(0, len(msg), 4000)]
                for i, chunk in enumerate(chunks, 1):
                    await update.message.reply_text(f"{chunk}\n\n*(Phần {i}/{len(chunks)})*", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: {str(e)}")

    @auto_update_user
    async def setup_group_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if user_id != OWNER_ID:
            await update.message.reply_text("❌ Chỉ chủ sở hữu bot mới có thể setup group!")
            return
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong group!")
            return
        
        if set_group_owner(chat_id, OWNER_ID):
            await update.message.reply_text(f"✅ *THIẾT LẬP GROUP THÀNH CÔNG*\n━━━━━━━━━━━━━━━━\n\n• Group này đã được đặt dưới quyền sở hữu của bạn\n• Tất cả dữ liệu trong group sẽ là của bạn\n• Bạn có thể thêm admin để cùng quản lý\n\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Lỗi khi thiết lập group!")

    @auto_update_user
    async def group_info_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong group!")
            return
        
        owner_id = get_group_owner(chat_id)
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (owner_id,))
        owner_info = c.fetchone()
        conn.close()
        
        owner_display = f"@{owner_info[0]}" if owner_info and owner_info[0] else (owner_info[1] if owner_info else f"User {owner_id}")
        
        msg = (f"ℹ️ *THÔNG TIN GROUP*\n━━━━━━━━━━━━━━━━\n\n"
               f"• Group ID: `{chat_id}`\n"
               f"• Chủ sở hữu: {owner_display} (`{owner_id}`)\n"
               f"• Bạn: {update.effective_user.first_name} (`{update.effective_user.id}`)\n\n"
               f"🕐 {format_vn_time()}")
        
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @auto_update_user
    async def add_group_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
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
            await update.message.reply_text(f"✅ Đã thêm @{target} làm admin với quyền {perm_type}!")
        else:
            await update.message.reply_text("❌ Lỗi khi thêm admin!")

    @auto_update_user
    @require_group_permission('manage')
    async def add_admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not ctx.args:
            await update.message.reply_text("📝 *HƯỚNG DẪN THÊM ADMIN*\n\n• `/addadmin @user view` - Thêm quyền xem\n• `/addadmin @user edit` - Thêm quyền sửa\n• `/addadmin @user delete` - Thêm quyền xóa\n• `/addadmin @user manage` - Thêm quyền quản lý\n• `/addadmin @user full` - Thêm toàn quyền\n\nVí dụ: `/addadmin @john view`", parse_mode=ParseMode.MARKDOWN)
            return
        
        target = ctx.args[0]
        perm_type = ctx.args[1] if len(ctx.args) > 1 else 'view'
        
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
            await update.message.reply_text(f"✅ Đã thêm {target} làm admin với quyền {perm_type}!")
        else:
            await update.message.reply_text("❌ Lỗi khi thêm admin!")

    def get_all_admins(group_id):
        """Lấy danh sách admin từ bảng permissions (KHÔNG phải group_admins)"""
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            # Đọc từ bảng permissions - đây là bảng đang được dùng để cấp quyền
            c.execute('''
                SELECT p.user_id, p.can_view_all, p.can_edit_all, p.can_delete_all, 
                       p.can_manage_perms, u.username, u.first_name, p.created_at 
                FROM permissions p 
                LEFT JOIN users u ON p.user_id = u.user_id 
                WHERE p.group_id = ? AND p.role = 'staff'
                ORDER BY p.created_at
            ''', (group_id,))
            admins = c.fetchall()
            conn.close()
            logger.info(f"📋 Found {len(admins)} admins in group {group_id} from permissions table")
            return admins
        except Exception as e:
            logger.error(f"❌ Lỗi get_all_admins: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def grant_admin_permission(group_id, admin_id, granted_by, permissions):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            created_at = get_vn_time().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute("DELETE FROM group_admins WHERE group_id = ? AND admin_id = ?", (group_id, admin_id))
            
            c.execute('''INSERT INTO group_admins (group_id, admin_id, granted_by, can_view, can_edit, can_delete, can_manage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
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
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM group_admins WHERE group_id = ? AND admin_id = ?", (group_id, admin_id))
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
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''SELECT can_view, can_edit, can_delete, can_manage FROM group_admins WHERE group_id = ? AND admin_id = ?''', (group_id, admin_id))
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

    @auto_update_user
    @require_group_permission('view')
    async def list_admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        admins = get_all_admins(chat_id)
        
        if not admins:
            await update.message.reply_text("📭 Chưa có admin nào trong group!")
            return
        
        msg = "👑 *DANH SÁCH ADMIN*\n━━━━━━━━━━━━━━━━\n\n"
        for admin in admins:
            if len(admin) >= 7:
                admin_id, view, edit, delete, manage, username, first_name = admin
            else:
                admin_id, view, edit, delete, manage = admin[:5]
                username = None
                first_name = None
            
            if username:
                display = f"@{username}"
            elif first_name:
                display = first_name
            else:
                display = f"User {admin_id}"
            
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
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        if not ctx.args:
            await update.message.reply_text("📝 *HƯỚNG DẪN XÓA ADMIN*\n\n• `/removeadmin @user` - Xóa admin\n• `/removeadmin ID` - Xóa admin bằng ID\n\nVí dụ: `/removeadmin @john`", parse_mode=ParseMode.MARKDOWN)
            return
        
        target = ctx.args[0]
        
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
        
        if admin_id == update.effective_user.id:
            await update.message.reply_text("❌ Không thể tự xóa quyền admin của chính mình!")
            return
        
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
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        if not ctx.args:
            categories = get_expense_categories(owner_id)
            
            if not categories:
                await update.message.reply_text("📭 Chưa có danh mục nào để xóa!")
                return
            
            msg = "🗑 *CHỌN DANH MỤC CẦN XÓA*\n━━━━━━━━━━━━━━━━\n\n"
            keyboard = []
            row = []
            
            for i, cat in enumerate(categories, 1):
                cat_id, name, budget, created = cat
                safe_name = escape_markdown(name)  # THÊM DÒNG NÀY
                msg += f"{i}. *{safe_name}* - {format_currency_simple(budget, 'VND')}\n"
                
                # ĐẢM BẢO callback_data là string
                callback_data = f"del_cat_{cat_id}"
                row.append(InlineKeyboardButton(f"{i}", callback_data=callback_data))
                if len(row) == 5:
                    keyboard.append(row)
                    row = []
            
            if row:
                keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")])
            
            msg += f"\n🕐 {format_vn_time_short()}"
            
            # SỬA DÒNG NÀY - dùng safe_edit_message thay vì reply_text trực tiếp
            safe_msg = escape_markdown(msg)
            await update.message.reply_text(safe_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        try:
            category_id = int(ctx.args[0])
            
            keyboard = [[InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"confirm_del_cat_{category_id}"),
                         InlineKeyboardButton("❌ Hủy", callback_data="expense_categories")]]
            
            categories = get_expense_categories(owner_id)
            category_name = "Không xác định"
            for cat in categories:
                if cat[0] == category_id:
                    category_name = cat[1]
                    break
            
            # ESCAPE tên danh mục
            safe_category_name = escape_markdown(category_name)
            
            msg = (f"⚠️ *CẢNH BÁO: XÓA DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                   f"📋 Danh mục: *{safe_category_name}* (ID: {category_id})\n\n"
                   f"❗️ Hành động này sẽ xóa:\n"
                   f"• Danh mục *{safe_category_name}*\n"
                   f"• Tất cả chi tiêu trong danh mục này\n\n"
                   f"❌ *Không thể khôi phục!*\n\n"
                   f"Bạn có chắc chắn muốn xóa?")
            
            safe_msg = escape_markdown(msg)
            await update.message.reply_text(safe_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            
        except ValueError:
            await update.message.reply_text("❌ ID không hợp lệ!")

    @auto_update_user
    @require_group_permission('delete')
    async def quick_delete_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ Hãy reply tin nhắn chứa ID danh mục cần xóa!")
            return
        
        reply_text = update.message.reply_to_message.text
        match = re.search(r'\*(\d+)\.\*', reply_text) or re.search(r'ID: (\d+)', reply_text)
        
        if not match:
            await update.message.reply_text("❌ Không tìm thấy ID danh mục trong tin nhắn được reply!")
            return
        
        category_id = int(match.group(1))
        owner_id = ctx.bot_data.get('effective_user_id', update.effective_user.id)
        
        keyboard = [[InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"confirm_del_cat_{category_id}"),
                     InlineKeyboardButton("❌ Hủy", callback_data="expense_categories")]]
        
        msg = f"⚠️ *XÁC NHẬN XÓA DANH MỤC #{category_id}*\n\nBạn có chắc chắn muốn xóa?"
        safe_msg = escape_markdown(msg)
        await update.message.reply_text(safe_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    @auto_update_user
    @require_permission('view')
    async def balance_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        
        await msg.edit_text(balance_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    @auto_update_user
    async def show_portfolio_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        current_user = query.from_user.id
        chat_id = query.message.chat.id
        chat_type = query.message.chat.type
        
        if chat_type in ['group', 'supergroup']:
            owner_id = get_group_owner(chat_id)
            
            if current_user != owner_id and not check_permission(chat_id, current_user, 'view'):
                await query.edit_message_text("❌ Bạn không có quyền xem portfolio!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]))
                return
            
            target_user_id = owner_id
            target_name = "của group"
        else:
            target_user_id = current_user
            target_name = "của bạn"
        
        portfolio_data = get_portfolio(target_user_id)
        
        if not portfolio_data:
            await query.edit_message_text(f"📭 Danh mục {target_name} trống!\n\n🕐 {format_vn_time()}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]))
            return
        
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
        
        msg = f"📊 *DANH MỤC {target_name}*\n━━━━━━━━━━━━━━━━\n\n"
        
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
        
        keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
        
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    async def perm_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        if chat_type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm!")
            return
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM permissions WHERE group_id = ?", (chat_id,))
        admin_count = c.fetchone()[0]
        
        if admin_count == 0:
            permissions = {'view': 1, 'edit': 1, 'delete': 1, 'manage': 1}
            if grant_permission(chat_id, user_id, user_id, permissions):
                await update.message.reply_text("👑 *BẠN LÀ ADMIN ĐẦU TIÊN*\n\n✅ Đã tự động cấp toàn quyền!\nDùng `/perm list` để xem danh sách.", parse_mode=ParseMode.MARKDOWN)
                await update_user_info_async(update.effective_user)
                conn.close()
                return
        
        conn.close()
        
        if not check_permission(chat_id, user_id, 'manage'):
            await update.message.reply_text("❌ Bạn không có quyền quản lý phân quyền!")
            return
        
        if not ctx.args:
            msg = ("🔐 *QUẢN LÝ PHÂN QUYỀN*\n━━━━━━━━━━━━━━━━\n\n"
                   "*Các lệnh:*\n"
                   "• `/perm list` - Xem danh sách admin\n"
                   "• `/perm grant @user view` - Cấp quyền xem\n"
                   "• `/perm grant @user edit` - Cấp quyền sửa\n"
                   "• `/perm grant @user delete` - Cấp quyền xóa\n"
                   "• `/perm grant @user manage` - Cấp quyền quản lý\n"
                   "• `/perm grant @user full` - Cấp toàn quyền\n"
                   "• `/perm revoke @user` - Thu hồi quyền\n\n"
                   f"🕐 {format_vn_time_short()}")
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        if ctx.args[0] == "list":
            admins = get_all_admins(chat_id)
            if not admins:
                # Thử lấy từ bảng cũ nếu không có
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT COUNT(*) FROM group_admins WHERE group_id = ?''', (chat_id,))
                old_count = c.fetchone()[0]
                conn.close()
                
                if old_count > 0:
                    await update.message.reply_text("⚠️ Phát hiện dữ liệu admin cũ! Đang migrate...")
                    migrate_admin_data()
                    admins = get_all_admins(chat_id)  # Thử lại
                    
                if not admins:
                    await update.message.reply_text("📭 Chưa có admin nào được cấp quyền!")
                    return
            
            msg = "👑 *DANH SÁCH ADMIN*\n━━━━━━━━━━━━━━━━\n\n"
            for admin in admins:
                if len(admin) >= 7:
                    user_id, view, edit, delete, manage, username, first_name = admin
                else:
                    user_id, view, edit, delete, manage = admin[:5]
                    username = None
                    first_name = None
                
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
            
            if target.startswith('@'):
                username = target[1:]
                target_id = get_user_id_by_username(username)
                
                if not target_id:
                    try:
                        chat = await ctx.bot.get_chat(username)
                        if chat:
                            target_id = chat.id
                            await update_user_info_async(chat)
                    except Exception as e:
                        logger.error(f"Lỗi get_chat: {e}")
                    
                    if not target_id:
                        await update.message.reply_text(f"❌ Không tìm thấy user {target}\n\n💡 *Cách khắc phục:*\n1. Yêu cầu user @{username} nhắn tin cho bot\n2. Hoặc dùng ID trực tiếp: `/perm grant [ID] {perm_type}`\n3. Dùng `/whoami` để xem ID của bạn\n4. Hoặc reply tin nhắn của họ và dùng: `/permgrant {perm_type}`", parse_mode=ParseMode.MARKDOWN)
                        return
            else:
                try:
                    target_id = int(target)
                    if not get_user_id_by_username(str(target_id)):
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

    async def expense_shortcut_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Xác định user_id thực sự
        chat_type = update.effective_chat.type
        current_user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if chat_type == 'private':
            # Private chat: thêm cho chính mình
            target_user_id = current_user_id
            logger.info(f"💬 PRIVATE: thêm chi tiêu cho user {target_user_id}")
        else:
            # Group chat: thêm cho chủ sở hữu (nếu có quyền)
            target_user_id = ctx.bot_data.get('effective_user_id', current_user_id)
            logger.info(f"👥 GROUP: thêm chi tiêu cho owner {target_user_id}")
        
        text = update.message.text.strip()
        
        # ==================== THÊM THU NHẬP (tn) ====================
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
                
                if add_income(target_user_id, amount, source, currency, note):
                    # Thông báo ai là người sở hữu
                    owner_info = ""
                    if chat_type != 'private' and target_user_id != current_user_id:
                        owner_info = "\n📌 Dữ liệu thuộc về chủ sở hữu group"
                    
                    await update.message.reply_text(
                        f"✅ *ĐÃ THÊM THU NHẬP*\n━━━━━━━━━━━━━━━━\n\n"
                        f"💰 Số tiền: *{format_currency_simple(amount, currency)}*\n"
                        f"📌 Nguồn: *{source}*\n"
                        f"📝 Ghi chú: *{note if note else 'Không có'}*{owner_info}\n\n"
                        f"🕐 {format_vn_time()}", 
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text("❌ Lỗi khi thêm thu nhập!")
            except ValueError:
                await update.message.reply_text("❌ Số tiền không hợp lệ!")
        
        # ==================== THÊM DANH MỤC (dm) ====================
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
            
            if add_expense_category(target_user_id, name, budget):
                owner_info = ""
                if chat_type != 'private' and target_user_id != current_user_id:
                    owner_info = "\n📌 Dữ liệu thuộc về chủ sở hữu group"
                
                await update.message.reply_text(
                    f"✅ *ĐÃ THÊM DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                    f"📋 Tên: *{name.upper()}*\n"
                    f"💰 Budget: {format_currency_simple(budget, 'VND')}{owner_info}\n\n"
                    f"🕐 {format_vn_time()}", 
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text("❌ Lỗi khi thêm danh mục!")
        
        # ==================== THÊM CHI TIÊU (ct) ====================
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
                
                categories = get_expense_categories(target_user_id)
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
                
                if add_expense(target_user_id, category_id, amount, currency, note):
                    owner_info = ""
                    if chat_type != 'private' and target_user_id != current_user_id:
                        owner_info = "\n📌 Dữ liệu thuộc về chủ sở hữu group"
                    
                    await update.message.reply_text(
                        f"✅ *ĐÃ THÊM CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n"
                        f"💰 Số tiền: *{format_currency_simple(amount, currency)}*\n"
                        f"📂 Danh mục: *{category_name}*\n"
                        f"📝 Ghi chú: *{note if note else 'Không có'}*{owner_info}\n\n"
                        f"🕐 {format_vn_time()}", 
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text("❌ Lỗi khi thêm chi tiêu!")
            except ValueError:
                await update.message.reply_text("❌ ID hoặc số tiền không hợp lệ!")
        
        # ==================== XEM GIAO DỊCH GẦN ĐÂY (ds) ====================
        elif text == 'ds':
            recent_incomes = get_recent_incomes(target_user_id, 10)
            recent_expenses = get_recent_expenses(target_user_id, 10)
            
            if not recent_incomes and not recent_expenses:
                await update.message.reply_text("📭 Chưa có giao dịch nào!")
                return
            
            msg = "🔄 *GIAO DỊCH GẦN ĐÂY*\n━━━━━━━━━━━━━━━━\n\n"
            
            # Thêm thông tin chủ sở hữu nếu đang ở group
            if chat_type != 'private' and target_user_id != current_user_id:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
                owner_info = c.fetchone()
                conn.close()
                owner_name = f"@{owner_info[0]}" if owner_info and owner_info[0] else (owner_info[1] if owner_info else f"User {target_user_id}")
                msg += f"📌 Dữ liệu của: {owner_name}\n\n"
            
            if recent_incomes:
                msg += "*💰 THU NHẬP:*\n"
                for inc in recent_incomes:
                    inc_id, amount, source, note, date, currency = inc
                    msg += f"• #{inc_id} {date}: {format_currency_simple(amount, currency)} - {source}\n"
                    if note:
                        msg += f"  📝 {note}\n"
                msg += "\n"
            
            if recent_expenses:
                msg += "*💸 CHI TIÊU:*\n"
                for exp in recent_expenses:
                    exp_id, cat_name, amount, note, date, currency = exp
                    msg += f"• #{exp_id} {date}: {format_currency_simple(amount, currency)} - {cat_name}\n"
                    if note:
                        msg += f"  📝 {note}\n"
            
            msg += f"\n🕐 {format_vn_time()}"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        
        # ==================== BÁO CÁO THÁNG (bc) ====================
        elif text == 'bc':
            incomes_data = get_income_by_period(target_user_id, 'month')
            expenses_data = get_expenses_by_period(target_user_id, 'month')
            
            msg = f"📊 *BÁO CÁO THÁNG {get_vn_time().strftime('%m/%Y')}*\n━━━━━━━━━━━━━━━━\n\n"
            
            # Thêm thông tin chủ sở hữu nếu đang ở group
            if chat_type != 'private' and target_user_id != current_user_id:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
                owner_info = c.fetchone()
                conn.close()
                owner_name = f"@{owner_info[0]}" if owner_info and owner_info[0] else (owner_info[1] if owner_info else f"User {target_user_id}")
                msg += f"📌 Dữ liệu của: {owner_name}\n\n"
            
            if incomes_data['transactions']:
                msg += "*💰 THU NHẬP:*\n"
                for inc in incomes_data['transactions'][:10]:
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
                for exp in expenses_data['transactions'][:10]:
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
        
        # ==================== XÓA CHI TIÊU (xoa chi) ====================
        elif text.startswith('xoa chi '):
            parts = text.split()
            if len(parts) < 3:
                await update.message.reply_text("❌ Cần có ID! VD: `xoa chi 5`")
                return
            
            try:
                expense_id = int(parts[2])
                if delete_expense(expense_id, target_user_id):
                    owner_info = ""
                    if chat_type != 'private' and target_user_id != current_user_id:
                        owner_info = " của chủ sở hữu"
                    await update.message.reply_text(
                        f"✅ Đã xóa khoản chi{owner_info} #{expense_id}\n\n🕐 {format_vn_time_short()}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text(f"❌ Không tìm thấy khoản chi #{expense_id}")
            except ValueError:
                await update.message.reply_text("❌ ID không hợp lệ!")
        
        # ==================== XÓA THU NHẬP (xoa thu) ====================
        elif text.startswith('xoa thu '):
            parts = text.split()
            if len(parts) < 3:
                await update.message.reply_text("❌ Cần có ID! VD: `xoa thu 3`")
                return
            
            try:
                income_id = int(parts[2])
                if delete_income(income_id, target_user_id):
                    owner_info = ""
                    if chat_type != 'private' and target_user_id != current_user_id:
                        owner_info = " của chủ sở hữu"
                    await update.message.reply_text(
                        f"✅ Đã xóa khoản thu{owner_info} #{income_id}\n\n🕐 {format_vn_time_short()}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text(f"❌ Không tìm thấy khoản thu #{income_id}")
            except ValueError:
                await update.message.reply_text("❌ ID không hợp lệ!")
        
        # ==================== SỬA THU NHẬP (edit thu / sua thu) ====================
        elif text.startswith('edit thu ') or text.startswith('sua thu '):
            # Chuyển thành lệnh /editthu
            if text.startswith('edit thu '):
                fake_args = text.replace('edit thu ', '').split()
            else:
                fake_args = text.replace('sua thu ', '').split()
            ctx.args = fake_args
            await edit_income_command(update, ctx)
        
        # ==================== SỬA CHI TIÊU (edit chi / sua chi) ====================
        elif text.startswith('edit chi ') or text.startswith('sua chi '):
            # Chuyển thành lệnh /editchi
            if text.startswith('edit chi '):
                fake_args = text.replace('edit chi ', '').split()
            else:
                fake_args = text.replace('sua chi ', '').split()
            ctx.args = fake_args
            await edit_expense_command(update, ctx)

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user:
            await update_user_info_async(update.effective_user)
        
        text = update.message.text.strip()
        chat_type = update.effective_chat.type
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        logger.info(f"📨 Tin nhắn từ user {user_id} trong {chat_type}: '{text}'")
        
        # TRONG GROUP: Kiểm tra quyền trước khi xử lý
        if chat_type in ['group', 'supergroup']:
            if not check_permission(chat_id, user_id, 'view'):
                logger.info(f"⛔ User {user_id} không có quyền trong group, bỏ qua")
                return
        
        # Xử lý tính toán nếu có
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
        
        # Xử lý các lệnh tắt (tn, dm, ct, ds, bc)
        if text.startswith(('tn ', 'dm ', 'ct ', 'ds', 'bc', 'xoa chi ', 'xoa thu ')):
            await expense_shortcut_handler(update, ctx)
            return
        
        # Xử lý menu chính - SO SÁNH CHÍNH XÁC
        if text == "💰 ĐẦU TƯ COIN":
            logger.info(f"💰 User {user_id} chọn menu ĐẦU TƯ COIN")
            await update.message.reply_text(
                f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}", 
                parse_mode=ParseMode.MARKDOWN, 
                reply_markup=get_invest_menu_keyboard(user_id, chat_id, chat_type)
            )
            return
            
        if text == "💵 QUẢN LÝ CHI TIÊU":
            logger.info(f"💰 User {user_id} chọn menu QUẢN LÝ CHI TIÊU")
            await update.message.reply_text(
                f"💰 *QUẢN LÝ CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}", 
                parse_mode=ParseMode.MARKDOWN, 
                reply_markup=get_expense_menu_keyboard()
            )
            return
            
        if text == "🤔 HƯỚNG DẪN":
            logger.info(f"❓ User {user_id} chọn HƯỚNG DẪN")
            await help_command(update, ctx)
            return
        
        # Nếu không khớp với bất kỳ điều kiện nào
        logger.info(f"❓ Tin nhắn không xác định: '{text}'")

    async def export_csv_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = ctx.bot_data.get('effective_user_id', query.from_user.id)
        
        await query.edit_message_text("🔄 Đang tạo file CSV...")
        
        try:
            if query.data == "export_csv":
                transactions = get_transaction_detail(user_id)
                if not transactions:
                    await query.edit_message_text(
                        "📭 Không có dữ liệu portfolio để xuất!", 
                        parse_mode=None,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]])
                    )
                    return
                
                timestamp = get_vn_time().strftime('%Y%m%d_%H%M%S')
                filename = f"portfolio_{user_id}_{timestamp}.csv"
                filepath = os.path.join(EXPORT_DIR, filename)
                
                logger.info(f"📝 Đang tạo file CSV: {filepath}")
                
                with open(filepath, 'w', newline='', encoding='utf-8-sig') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(['ID', 'Mã coin', 'Số lượng', 'Giá mua (USD)', 'Ngày mua', 'Tổng vốn (USD)'])
                    for tx in transactions:
                        writer.writerow([tx[0], tx[1], tx[2], tx[3], tx[4], tx[5]])
                
                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    logger.info(f"✅ File đã tạo: {filepath}, kích thước: {file_size} bytes")
                    
                    with open(filepath, 'rb') as f:
                        await query.message.reply_document(document=f, filename=filename, caption=f"📊 *BÁO CÁO DANH MỤC ĐẦU TƯ*\n━━━━━━━━━━━━━━━━\n\n✅ Xuất thành công {len(transactions)} giao dịch!\n📁 File: `{filename}`\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN)
                    
                    os.remove(filepath)
                    logger.info(f"🗑 Đã xóa file tạm: {filepath}")
                else:
                    logger.error(f"❌ Không tìm thấy file sau khi tạo: {filepath}")
                    await query.edit_message_text("❌ Lỗi: Không thể tạo file CSV!")
                    return
                
                await query.edit_message_text(f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN, reply_markup=get_invest_menu_keyboard(user_id, query.message.chat.id))
                
            elif query.data == "expense_export":
                expenses = get_recent_expenses(user_id, 1000)
                incomes = get_recent_incomes(user_id, 1000)
                
                if not expenses and not incomes:
                    await query.edit_message_text(
                        "📭 Không có dữ liệu chi tiêu để xuất!", 
                        parse_mode=None,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]])
                    )
                    return
                
                timestamp = get_vn_time().strftime('%Y%m%d_%H%M%S')
                filename = f"expense_report_{user_id}_{timestamp}.csv"
                filepath = os.path.join(EXPORT_DIR, filename)
                
                logger.info(f"📝 Đang tạo file CSV: {filepath}")
                
                with open(filepath, 'w', newline='', encoding='utf-8-sig') as csvfile:
                    writer = csv.writer(csvfile)
                    
                    writer.writerow(['=== THU NHẬP ==='])
                    writer.writerow(['ID', 'Ngày', 'Nguồn', 'Số tiền', 'Loại tiền', 'Ghi chú'])
                    for inc in incomes:
                        writer.writerow([inc[0], inc[4], inc[2], inc[1], inc[5], inc[3]])
                    
                    writer.writerow([])
                    
                    writer.writerow(['=== CHI TIÊU ==='])
                    writer.writerow(['ID', 'Ngày', 'Danh mục', 'Số tiền', 'Loại tiền', 'Ghi chú'])
                    for exp in expenses:
                        writer.writerow([exp[0], exp[4], exp[1], exp[2], exp[5], exp[3]])
                
                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    logger.info(f"✅ File đã tạo: {filepath}, kích thước: {file_size} bytes")
                    
                    with open(filepath, 'rb') as f:
                        await query.message.reply_document(document=f, filename=filename, caption=f"📊 *BÁO CÁO THU CHI*\n━━━━━━━━━━━━━━━━\n\n✅ Xuất thành công!\n• Thu nhập: {len(incomes)} giao dịch\n• Chi tiêu: {len(expenses)} giao dịch\n📁 File: `{filename}`\n\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN)
                    
                    os.remove(filepath)
                    logger.info(f"🗑 Đã xóa file tạm: {filepath}")
                else:
                    logger.error(f"❌ Không tìm thấy file sau khi tạo: {filepath}")
                    await query.edit_message_text("❌ Lỗi: Không thể tạo file CSV!")
                    return
                
                await query.edit_message_text(f"💰 *QUẢN LÝ CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}", parse_mode=ParseMode.MARKDOWN, reply_markup=get_expense_menu_keyboard())
        except Exception as e:
            logger.error(f"❌ Lỗi export CSV: {e}", exc_info=True)
            await query.edit_message_text(
                f"❌ Lỗi khi xuất CSV: {str(e)[:200]}", 
                parse_mode=None,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_main")]])
            )
            
            try:
                if 'filepath' in locals() and os.path.exists(filepath):
                    os.remove(filepath)
                    logger.info(f"🗑 Đã dọn dẹp file lỗi: {filepath}")
            except:
                pass

    async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        # Log chi tiết
        logger.info("=" * 50)
        logger.info(f"🔔 CALLBACK NHẬN ĐƯỢC: {query.data}")
        logger.info(f"   • User: {query.from_user.id} (@{query.from_user.username})")
        logger.info(f"   • Chat: {query.message.chat.id}")
        logger.info(f"   • Message ID: {query.message.message_id}")
        logger.info("=" * 50)
        
        if query.from_user:
            await update_user_info_async(query.from_user)
        
        data = query.data
        
        try:
            # Lấy thông tin cơ bản
            current_user_id = query.from_user.id
            chat_id = query.message.chat.id
            chat_type = query.message.chat.type
            
            # ===== QUAN TRỌNG: XÁC ĐỊNH TARGET USER DỰA TRÊN CHAT TYPE =====
            if chat_type == 'private':
                # PRIVATE CHAT: LUÔN XỬ LÝ DỮ LIỆU CỦA CHÍNH USER
                target_user_id = current_user_id
                is_admin = False
                is_owner_user = False
                owner_id = current_user_id  # Trong private, owner là chính mình
                logger.info(f"💬 PRIVATE CALLBACK: xử lý cho user {target_user_id}")
            else:
                # GROUP CHAT: Lấy thông tin từ context
                owner_id = ctx.bot_data.get('group_owner_id', get_group_owner(chat_id))
                is_admin = ctx.bot_data.get('is_admin', False)
                is_owner_user = (current_user_id == owner_id)
                
                # Kiểm tra quyền trong group
                if not check_permission(chat_id, current_user_id, 'view'):
                    await safe_edit_message(query, "❌ Bạn không có quyền sử dụng bot trong nhóm này!")
                    return
                
                # Xác định target_user_id
                if is_admin or is_owner_user:
                    target_user_id = owner_id
                    logger.info(f"👥 GROUP CALLBACK: admin {current_user_id} xử lý dữ liệu owner {target_user_id}")
                else:
                    target_user_id = current_user_id
                    logger.info(f"👥 GROUP CALLBACK: user {current_user_id} tự xử lý dữ liệu")
            
            logger.info(f"🎯 Target user: {target_user_id}, IsAdmin: {is_admin}, IsOwner: {is_owner_user}")
            
            # ===========================================
            # NHÓM 1: XỬ LÝ XÓA DANH MỤC (ƯU TIÊN CAO NHẤT)
            # ===========================================
            
            if data.startswith("confirm_del_cat_"):
                cat_id = data.replace("confirm_del_cat_", "")
                logger.info(f"📂 Xác nhận xóa danh mục ID: {cat_id}")
                
                try:
                    category_id = int(cat_id)
                except ValueError:
                    await safe_edit_message(query, "❌ ID danh mục không hợp lệ!")
                    return
                
                await query.edit_message_text("🔄 Đang xóa danh mục...", parse_mode=None)
                
                try:
                    success, result, deleted_count = delete_category(category_id, owner_id)
                    
                    if success:
                        safe_result = escape_markdown(str(result))
                        msg = (f"✅ *ĐÃ XÓA DANH MỤC*\n"
                               f"━━━━━━━━━━━━━━━━\n\n"
                               f"📋 Đã xóa danh mục: *{safe_result}*\n"
                               f"💰 Đã xóa *{deleted_count}* khoản chi\n\n"
                               f"🕐 {format_vn_time()}")
                    else:
                        safe_result = escape_markdown(str(result))
                        msg = (f"❌ *LỖI*\n"
                               f"━━━━━━━━━━━━━━━━\n\n"
                               f"{safe_result}\n\n"
                               f"🕐 {format_vn_time()}")
                    
                    safe_msg = escape_markdown(msg)
                    keyboard = [[
                        InlineKeyboardButton("📋 Xem danh mục", callback_data="expense_categories"),
                        InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")
                    ]]
                    
                    await safe_edit_message(query, safe_msg, reply_markup=InlineKeyboardMarkup(keyboard))
                    
                except Exception as e:
                    logger.error(f"❌ Lỗi xóa danh mục: {e}")
                    await query.edit_message_text(f"❌ Lỗi: {str(e)[:100]}", parse_mode=None)
                
                return
            
            if data.startswith("del_cat_"):
                cat_id = data.replace("del_cat_", "")
                logger.info(f"📂 Xóa danh mục ID: {cat_id}")
                
                try:
                    category_id = int(cat_id)
                except ValueError:
                    await safe_edit_message(query, "❌ ID danh mục không hợp lệ!")
                    return
                
                categories = get_expense_categories(owner_id)
                category_name = "Không xác định"
                for cat in categories:
                    if cat[0] == category_id:
                        category_name = cat[1]
                        break
                
                safe_category_name = escape_markdown(category_name)
                
                keyboard = [[
                    InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"confirm_del_cat_{category_id}"),
                    InlineKeyboardButton("❌ Hủy", callback_data="expense_categories")
                ]]
                
                msg = (f"⚠️ *CẢNH BÁO: XÓA DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                       f"📋 Danh mục: *{safe_category_name}* (ID: {category_id})\n\n"
                       f"❗️ Hành động này sẽ xóa:\n"
                       f"• Danh mục *{safe_category_name}*\n"
                       f"• Tất cả chi tiêu trong danh mục này\n\n"
                       f"❌ *Không thể khôi phục!*\n\n"
                       f"Bạn có chắc chắn muốn xóa?")
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # NHÓM 2: XỬ LÝ XÓA GIAO DỊCH COIN (ƯU TIÊN CAO)
            # ===========================================
            
            if data.startswith("confirm_del_"):
                tx_id_str = data.replace("confirm_del_", "")
                logger.info(f"💰 Xác nhận xóa giao dịch: {tx_id_str}")
                
                # Kiểm tra nếu là số (xóa coin)
                if tx_id_str.isdigit():
                    tx_id = int(tx_id_str)
                    
                    # Kiểm tra giao dịch
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute('''SELECT user_id, symbol, amount FROM portfolio WHERE id = ?''', (tx_id,))
                    result = c.fetchone()
                    
                    if not result:
                        conn.close()
                        await safe_edit_message(query, f"❌ Không tìm thấy giao dịch #{tx_id}")
                        return
                    
                    tx_owner_id, symbol, amount = result
                    
                    # Chỉ cho phép xóa nếu là chủ sở hữu hoặc admin
                    can_delete = False
                    can_delete = False
                    if tx_owner_id == target_user_id:
                        can_delete = True
                    elif is_admin and chat_type != 'private':  # Trong group mới được admin xóa
                        can_delete = True

                    
                    if not can_delete:
                        conn.close()
                        await safe_edit_message(query, "❌ Bạn không có quyền xóa giao dịch này!")
                        return
                    
                    # Thực hiện xóa
                    c.execute('''DELETE FROM portfolio WHERE id = ?''', (tx_id,))
                    conn.commit()
                    conn.close()
                    
                    msg = (f"✅ *ĐÃ XÓA GIAO DỊCH #{tx_id}*\n━━━━━━━━━━━━━━━━\n\n"
                           f"• Coin: {symbol}\n"
                           f"• Số lượng: {amount:.4f}\n\n"
                           f"🕐 {format_vn_time()}")
                    
                    keyboard = [[InlineKeyboardButton("🔙 Về danh sách", callback_data="edit_transactions")]]
                    
                    await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                    return
                
                # Nếu là cat_ (xóa danh mục) - đã xử lý ở nhóm 1
                elif tx_id_str.startswith("cat_"):
                    return
                
                else:
                    await safe_edit_message(query, "❌ ID không hợp lệ!")
                    return
            
            # ===========================================
            # NHÓM 3: CÁC CALLBACK CHÍNH XÁC (MENU CHÍNH)
            # ===========================================
            
            if data == "edit_transactions":
                logger.info("📋 Hiển thị danh sách sửa/xóa giao dịch")
                
                # QUAN TRỌNG: Kiểm tra chat type
                chat_type = query.message.chat.type
                
                # Nếu là private chat, chỉ quản lý dữ liệu của chính mình
                if chat_type == 'private':
                    target_user_id = current_user_id
                    logger.info(f"💬 Private chat: quản lý giao dịch cá nhân {target_user_id}")
                else:
                    # Trong group, chỉ cho phép chủ sở hữu hoặc admin
                    if not is_owner_user and not is_admin:
                        await safe_edit_message(query, "❌ Bạn không có quyền quản lý giao dịch!")
                        return
                    target_user_id = owner_id
                
                transactions = get_transaction_detail(target_user_id)
                
                if not transactions:
                    msg = f"📭 Không có giao dịch!\n\n🕐 {format_vn_time()}"
                    keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                    await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                    return
                
                msg = "✏️ *CHỌN GIAO DỊCH*\n━━━━━━━━━━━━━━━━\n\n"
                keyboard = []
                row = []
                
                for tx in transactions:
                    tx_id, symbol, amount, price, date, total = tx
                    short_date = date.split()[0] if date else "N/A"
                    amount_str = f"{amount:.4f}".rstrip('0').rstrip('.') if '.' in f"{amount:.4f}" else f"{amount:.4f}"
                    
                    msg += f"• #{tx_id}: {symbol} {amount_str} @ {fmt_price(price)} ({short_date})\n"
                    
                    row.append(InlineKeyboardButton(f"#{tx_id}", callback_data=f"edit_{tx_id}"))
                    
                    if len(row) == 4:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                
                # Thay vì "Xem user khác", quay về menu chính
                keyboard.append([InlineKeyboardButton("🔙 Về menu đầu tư", callback_data="back_to_invest")])
                
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "back_to_main":
                msg = f"💰 *MENU CHÍNH*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}"
                await safe_edit_message(query, msg, reply_markup=None)
                await query.message.reply_text("👇 Chọn chức năng:", reply_markup=get_main_keyboard())
                return
            
            if data == "back_to_invest":
                uid = query.from_user.id
                gid = query.message.chat.id
                msg = f"💰 *MENU ĐẦU TƯ COIN*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}"
                await safe_edit_message(query, msg, reply_markup=get_invest_menu_keyboard(uid, gid, chat_type))
                return
            
            if data == "back_to_expense":
                msg = f"💰 *QUẢN LÝ CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n🕐 {format_vn_time()}"
                await safe_edit_message(query, msg, reply_markup=get_expense_menu_keyboard())
                return
            
            if data == "refresh_usdt":
                rate_data = get_usdt_vnd_rate()
                text = ("💱 *TỶ GIÁ USDT/VND*\n━━━━━━━━━━━━━━━━\n\n"
                        f"🇺🇸 *1 USDT* = `{fmt_vnd(rate_data['vnd'])}`\n"
                        f"🇻🇳 *1,000,000 VND* = `{1000000/rate_data['vnd']:.4f} USDT`\n\n"
                        f"⏱ *Cập nhật:* `{rate_data['update_time']}`\n"
                        f"📊 *Nguồn:* `{rate_data['source']}`\n\n"
                        f"🕐 {format_vn_time()}")
                keyboard = [[InlineKeyboardButton("🔄 Làm mới", callback_data="refresh_usdt")],
                            [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # NHÓM 4: XỬ LÝ XEM PORTFOLIO - CHỈ XEM CỦA CHỦ SỞ HỮU
            # ===========================================
            
            if data == "show_portfolio":
                logger.info("📊 Hiển thị portfolio")
                
                # Xác định user_id cần xem
                chat_type = query.message.chat.type
                current_user_id = query.from_user.id
                
                if chat_type == 'private':
                    target_user_id = current_user_id
                    logger.info(f"💬 Private: xem portfolio cá nhân {target_user_id}")
                else:
                    # Trong group, chỉ cho xem portfolio của chủ sở hữu nếu có quyền
                    owner_id = ctx.bot_data.get('group_owner_id', get_group_owner(chat_id))
                    is_admin = ctx.bot_data.get('is_admin', False)
                    is_owner = (current_user_id == owner_id)
                    
                    if not check_permission(chat_id, current_user_id, 'view'):
                        await safe_edit_message(query, "❌ Bạn không có quyền xem portfolio!")
                        return
                    
                    if is_admin or is_owner:
                        target_user_id = owner_id
                        logger.info(f"👥 Group: admin xem portfolio của owner {target_user_id}")
                    else:
                        target_user_id = current_user_id
                        logger.info(f"👥 Group: user xem portfolio cá nhân {target_user_id}")
                
                # Lấy dữ liệu portfolio
                portfolio_data = get_portfolio(target_user_id)
                
                if not portfolio_data:
                    msg = f"📭 Danh mục trống!\n\n🕐 {format_vn_time()}"
                    keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                    await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                    return
                
                # Lấy tất cả symbols để fetch giá
                symbols = list(set([row[0] for row in portfolio_data]))
                prices = get_prices_batch(symbols)
                
                # Tính toán tổng hợp theo từng coin
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
                
                # Lấy tên hiển thị
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (target_user_id,))
                user_info = c.fetchone()
                conn.close()
                
                display_name = user_info[0] if user_info and user_info[0] else (user_info[1] if user_info else f"User {target_user_id}")
                safe_display_name = escape_markdown(display_name)
                
                msg = f"📊 *DANH MỤC CỦA {safe_display_name}*\n━━━━━━━━━━━━━━━━\n\n"
                
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
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # NHÓM 5: XỬ LÝ XEM LỢI NHUẬN - CHỈ XEM CỦA CHỦ SỞ HỮU
            # ===========================================
            
            if data == "show_profit":
                logger.info("📈 Hiển thị lợi nhuận")
                
                # QUAN TRỌNG: Kiểm tra chat type
                chat_type = query.message.chat.type
                
                # Nếu là private chat, chỉ xem dữ liệu của chính mình
                if chat_type == 'private':
                    target_user_id = current_user_id
                    logger.info(f"💬 Private chat: xem lợi nhuận cá nhân {target_user_id}")
                else:
                    # Trong group, admin mới được xem dữ liệu chủ sở hữu
                    if not is_owner_user and not is_admin:
                        await safe_edit_message(query, "❌ Bạn không có quyền xem lợi nhuận!")
                        return
                    target_user_id = owner_id
                
                transactions = get_transaction_detail(target_user_id)
                
                if not transactions:
                    msg = f"📭 Danh mục trống!\n\n🕐 {format_vn_time()}"
                    keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                    await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                    return
                
                # Lấy tên hiển thị
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (owner_id,))
                user_info = c.fetchone()
                conn.close()
                
                display_name = user_info[0] if user_info and user_info[0] else (user_info[1] if user_info else f"User {owner_id}")
                safe_display_name = escape_markdown(display_name)
                
                msg = f"📈 *CHI TIẾT LỢI NHUẬN*\n━━━━━━━━━━━━━━━━\n\n"
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
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # NHÓM 6: XỬ LÝ XEM THỐNG KÊ - CHỈ XEM CỦA CHỦ SỞ HỮU
            # ===========================================
            
            if data == "show_stats":
                logger.info("📊 Hiển thị thống kê")
                
                # QUAN TRỌNG: Kiểm tra chat type
                chat_type = query.message.chat.type
                
                # Nếu là private chat, chỉ xem dữ liệu của chính mình
                if chat_type == 'private':
                    target_user_id = current_user_id
                    logger.info(f"💬 Private chat: xem thống kê cá nhân {target_user_id}")
                else:
                    # Trong group, admin mới được xem dữ liệu chủ sở hữu
                    if not is_owner_user and not is_admin:
                        await safe_edit_message(query, "❌ Bạn không có quyền xem thống kê!")
                        return
                    target_user_id = owner_id
                
                stats = get_portfolio_stats(target_user_id)
                
                if not stats:
                    msg = f"📭 Danh mục trống!"
                    keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                    await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                    return
                
                # Lấy tên hiển thị
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (owner_id,))
                user_info = c.fetchone()
                conn.close()
                
                display_name = user_info[0] if user_info and user_info[0] else (user_info[1] if user_info else f"User {owner_id}")
                safe_display_name = escape_markdown(display_name)
                
                msg = (f"📊 *THỐNG KÊ DANH MỤC*\n━━━━━━━━━━━━━━━━\n\n"
                       f"*TỔNG QUAN*\n"
                       f"• Vốn: `{fmt_price(stats['total_invest'])}`\n"
                       f"• Giá trị: `{fmt_price(stats['total_value'])}`\n"
                       f"• Lợi nhuận: `{fmt_price(stats['total_profit'])}`\n"
                       f"• Tỷ suất: `{stats['total_profit_percent']:+.2f}%`\n\n"
                       f"*📈 TOP COIN LỜI NHẤT*\n")
                
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
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # NHÓM 7: XỬ LÝ XEM GIÁ COIN
            # ===========================================
            
            if data.startswith("price_"):
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
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # NHÓM 8: XỬ LÝ SỬA GIAO DỊCH
            # ===========================================
            
            if data.startswith("edit_form_"):
                logger.info(f"📝 Form sửa giao dịch: {data}")
                tx_id_str = data.replace("edit_form_", "")
                
                if not tx_id_str.isdigit():
                    await safe_edit_message(query, "❌ ID không hợp lệ!")
                    return
                
                tx_id = int(tx_id_str)
                
                # Lấy thông tin giao dịch
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT symbol, amount, buy_price FROM portfolio WHERE id = ?''', (tx_id,))
                tx = c.fetchone()
                conn.close()
                
                if not tx:
                    await safe_edit_message(query, f"❌ Không tìm thấy giao dịch #{tx_id}")
                    return
                
                symbol, current_amount, current_price = tx
                
                msg = (f"✏️ *SỬA GIAO DỊCH #{tx_id}*\n━━━━━━━━━━━━━━━━\n\n"
                       f"*{symbol}*\n"
                       f"📊 SL hiện tại: `{current_amount:.4f}`\n"
                       f"💰 Giá hiện tại: `{fmt_price(current_price)}`\n\n"
                       f"*Nhập lệnh:*\n"
                       f"`/edit {tx_id} [số lượng mới] [giá mới]`\n\n"
                       f"*Ví dụ:*\n"
                       f"`/edit {tx_id} 0.5 45000`\n\n"
                       f"🕐 {format_vn_time_short()}")
                
                keyboard = [[InlineKeyboardButton("🔙 Quay lại", callback_data=f"edit_{tx_id}")]]
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
                
            if data.startswith("edit_"):
                logger.info(f"✏️ Sửa giao dịch: {data}")
                
                # QUAN TRỌNG: Kiểm tra nếu là "edit_transactions" thì đã xử lý ở trên
                if data == "edit_transactions":
                    return
                
                tx_id_str = data.replace("edit_", "")
                
                if not tx_id_str.isdigit():
                    logger.error(f"❌ edit_ callback với ID không hợp lệ: {tx_id_str}")
                    await safe_edit_message(query, "❌ ID không hợp lệ!")
                    return
                
                tx_id = int(tx_id_str)
                
                # Lấy chi tiết giao dịch
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT id, symbol, amount, buy_price, buy_date, total_cost, user_id 
                            FROM portfolio WHERE id = ?''', (tx_id,))
                tx = c.fetchone()
                conn.close()
                
                if not tx:
                    await safe_edit_message(query, f"❌ Không tìm thấy giao dịch #{tx_id}")
                    return
                
                tx_id, symbol, amount, price, date, total, tx_owner_id = tx
                
                # Kiểm tra quyền xem/sửa - chỉ cho phép chủ sở hữu hoặc admin
                if tx_owner_id != target_user_id and not is_admin:
                    await safe_edit_message(query, "❌ Bạn không có quyền xem giao dịch này!")
                    return
                
                # Lấy giá hiện tại
                price_data = get_price(symbol)
                current_price = price_data['p'] if price_data else 0
                profit = (current_price - price) * amount if current_price else 0
                profit_percent = ((current_price - price) / price) * 100 if price and current_price else 0
                
                # Tạo message
                msg = (f"📝 *GIAO DỊCH #{tx_id}*\n━━━━━━━━━━━━━━━━\n\n"
                       f"*{symbol}*\n"
                       f"📅 Ngày mua: {date}\n"
                       f"📊 Số lượng: `{amount:.4f}`\n"
                       f"💰 Giá mua: `{fmt_price(price)}`\n"
                       f"💵 Tổng vốn: `{fmt_price(total)}`\n"
                       f"📈 Giá hiện tại: `{fmt_price(current_price)}`\n"
                       f"{'✅' if profit>=0 else '❌'} Lợi nhuận: `{fmt_price(profit)}` ({profit_percent:+.2f}%)\n\n")
                
                # Thêm nút sửa/xóa nếu có quyền
                keyboard = []
                if tx_owner_id == owner_id or is_admin:
                    keyboard.append([
                        InlineKeyboardButton("✏️ Sửa", callback_data=f"edit_form_{tx_id}"),
                        InlineKeyboardButton("🗑 Xóa", callback_data=f"del_{tx_id}")
                    ])
                
                keyboard.append([InlineKeyboardButton("🔙 Về danh sách", callback_data="edit_transactions")])
                
                msg += f"🕐 {format_vn_time()}"
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
   
            if data.startswith("del_"):
                logger.info(f"🗑 Xóa giao dịch: {data}")
                tx_id_str = data.replace("del_", "")
                
                # Kiểm tra nếu là xóa danh mục (cat_) - đã xử lý ở nhóm 1
                if tx_id_str.startswith("cat_"):
                    return
                
                if not tx_id_str.isdigit():
                    await safe_edit_message(query, "❌ ID không hợp lệ!")
                    return
                
                tx_id = int(tx_id_str)
                
                # Kiểm tra giao dịch có tồn tại không
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''SELECT user_id, symbol, amount FROM portfolio WHERE id = ?''', (tx_id,))
                result = c.fetchone()
                conn.close()
                
                if not result:
                    await safe_edit_message(query, f"❌ Không tìm thấy giao dịch #{tx_id}")
                    return
                
                tx_owner_id, symbol, amount = result
                
                # Kiểm tra quyền xóa - chỉ cho phép chủ sở hữu hoặc admin
                can_delete = False
                can_delete = False
                if tx_owner_id == target_user_id:
                    can_delete = True
                elif is_admin and chat_type != 'private':  # Trong group mới được admin xóa
                    can_delete = True
                
                if not can_delete:
                    await safe_edit_message(query, "❌ Bạn không có quyền xóa giao dịch này!")
                    return
                
                # Hỏi xác nhận
                msg = (f"⚠️ *XÁC NHẬN XÓA*\n━━━━━━━━━━━━━━━━\n\n"
                       f"• Giao dịch: #{tx_id}\n"
                       f"• Coin: {symbol}\n"
                       f"• Số lượng: {amount:.4f}\n\n"
                       f"Bạn có chắc chắn muốn xóa?")
                
                keyboard = [[
                    InlineKeyboardButton("✅ Có", callback_data=f"confirm_del_{tx_id}"),
                    InlineKeyboardButton("❌ Không", callback_data=f"edit_{tx_id}")
                ]]
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # NHÓM 9: XỬ LÝ MENU CHI TIÊU
            # ===========================================
            
            if data == "show_alerts":
                uid = query.from_user.id
                alerts = get_user_alerts(uid)
                
                if not alerts:
                    msg = f"📭 Bạn chưa có cảnh báo nào!\n\n🕐 {format_vn_time()}"
                    await safe_edit_message(query, msg)
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
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "show_top10":
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
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "show_buy":
                msg = ("➕ *MUA COIN*\n\nDùng lệnh: `/buy [coin] [sl] [giá]`\n\n"
                       "*Ví dụ:*\n• `/buy btc 0.5 40000`\n\n"
                       f"🕐 {format_vn_time_short()}")
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "show_sell":
                msg = ("➖ *BÁN COIN*\n\nDùng lệnh: `/sell [coin] [sl]`\n\n"
                       "*Ví dụ:*\n• `/sell btc 0.2`\n\n"
                       f"🕐 {format_vn_time_short()}")
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "admin_panel":
                uid = query.from_user.id
                group_id = query.message.chat.id
                
                msg = ("👑 *ADMIN PANEL*\n━━━━━━━━━━━━━━━━\n\n"
                       "• `/perm list` - Danh sách admin\n"
                       "• `/perm grant @user view` - Cấp quyền xem\n"
                       "• `/perm grant @user edit` - Cấp quyền sửa\n"
                       "• `/perm grant @user delete` - Cấp quyền xóa\n"
                       "• `/perm grant @user manage` - Cấp quyền QL\n"
                       "• `/perm revoke @user` - Thu hồi quyền\n\n"
                       f"🕐 {format_vn_time()}")
                
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_invest")]]
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "expense_income_menu":
                msg = ("💰 *MENU THU NHẬP*\n\n"
                       "• `tn [số tiền]` - Thêm thu nhập\n"
                       "• `tn 100 USD Lương` - Thêm 100 USD\n\n"
                       f"🕐 {format_vn_time_short()}")
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "expense_expense_menu":
                msg = ("💸 *MENU CHI TIÊU*\n\n"
                       "• `ct [mã] [số tiền]` - Thêm chi tiêu\n"
                       "• `ct 1 50000 VND Ăn trưa` - Ví dụ\n\n"
                       f"🕐 {format_vn_time_short()}")
                keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "expense_categories":
                categories = get_expense_categories(owner_id)
                
                if not categories:
                    msg = (f"📋 Chưa có danh mục nào!\n"
                           f"Tạo: `dm [tên] [budget]`\n\n"
                           f"🕐 {format_vn_time_short()}")
                    keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                    await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                    return
                
                msg = "📋 *DANH MỤC CHI TIÊU*\n━━━━━━━━━━━━━━━━\n\n"
                keyboard = []
                row = []
                
                for cat in categories:
                    cat_id, name, budget, created = cat
                    safe_name = escape_markdown(name)
                    msg += f"• *{cat_id}.* {safe_name} - {format_currency_simple(budget, 'VND')}\n"
                    
                    row.append(InlineKeyboardButton(f"🗑 {cat_id}", callback_data=f"del_cat_{cat_id}"))
                    if len(row) == 4:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                
                keyboard.append([InlineKeyboardButton("➕ Thêm danh mục", callback_data="expense_expense_menu"),
                                 InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")])
                
                msg += f"\n🕐 {format_vn_time_short()}"
                
                await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            if data == "expense_today":
                try:
                    incomes_data = get_income_by_period(owner_id, 'day')
                    expenses_data = get_expenses_by_period(owner_id, 'day')
                    
                    msg = f"📅 *THU CHI HÔM NAY ({get_vn_time().strftime('%d/%m/%Y')})*\n━━━━━━━━━━━━━━━━\n\n"
                    
                    if incomes_data['transactions']:
                        msg += "*💰 THU NHẬP:*\n"
                        for inc in incomes_data['transactions']:
                            id, amount, source, note, currency, date = inc
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
                    
                    if len(msg) > 4000:
                        await query.edit_message_text("📊 *Báo cáo quá dài, đang chia nhỏ...*")
                        chunks = [msg[i:i+3500] for i in range(0, len(msg), 3500)]
                        for i, chunk in enumerate(chunks, 1):
                            await query.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                    else:
                        keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                        await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                        
                except Exception as e:
                    logger.error(f"Lỗi expense_today: {e}", exc_info=True)
                    await safe_edit_message(query, "❌ Có lỗi xảy ra khi xem hôm nay!")
                return
            
            if data == "expense_month":
                try:
                    incomes_data = get_income_by_period(owner_id, 'month')
                    expenses_data = get_expenses_by_period(owner_id, 'month')
                    
                    msg = f"📅 *THU CHI THÁNG {get_vn_time().strftime('%m/%Y')}*\n━━━━━━━━━━━━━━━━\n\n"
                    
                    if incomes_data['transactions']:
                        msg += "*💰 THU NHẬP:*\n"
                        for inc in incomes_data['transactions'][:10]:
                            id, amount, source, note, currency, date = inc
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
                    
                    if len(msg) > 4000:
                        await query.edit_message_text("📊 *Báo cáo quá dài, đang chia nhỏ...*")
                        chunks = [msg[i:i+3500] for i in range(0, len(msg), 3500)]
                        for i, chunk in enumerate(chunks, 1):
                            await query.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                    else:
                        keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                        await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                        
                except Exception as e:
                    logger.error(f"Lỗi expense_month: {e}", exc_info=True)
                    await safe_edit_message(query, "❌ Có lỗi xảy ra!")
                return
            
            if data == "expense_recent":
                try:
                    recent_incomes = get_recent_incomes(owner_id, 20)
                    recent_expenses = get_recent_expenses(owner_id, 20)
                    
                    if not recent_incomes and not recent_expenses:
                        msg = f"📭 Chưa có giao dịch nào!\n\n🕐 {format_vn_time_short()}"
                        keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                        await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    
                    msg = f"🔄 *20 GIAO DỊCH GẦN ĐÂY*\n━━━━━━━━━━━━━━━━\n\n"
                    
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
                    
                    if len(msg) > 4000:
                        await query.edit_message_text("📊 *Danh sách quá dài, đang chia nhỏ...*")
                        chunks = [msg[i:i+3500] for i in range(0, len(msg), 3500)]
                        for i, chunk in enumerate(chunks, 1):
                            await query.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                    else:
                        keyboard = [[InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]]
                        await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                        
                except Exception as e:
                    logger.error(f"Lỗi expense_recent: {e}", exc_info=True)
                    await safe_edit_message(query, "❌ Có lỗi xảy ra!")
                return
            
            if data == "export_csv" or data == "expense_export":
                await export_csv_handler(update, ctx)
                return
    
            if data.startswith("balance_"):
                period = data.replace("balance_", "")
                
                balance_data = get_balance_summary(owner_id, period)
                
                if not balance_data:
                    msg = "❌ Không thể tính cân đối!"
                    await safe_edit_message(query, msg)
                    return
                
                balance_msg = format_balance_message(balance_data, "")
                
                keyboard = [
                    [InlineKeyboardButton("📅 Hôm nay", callback_data="balance_day"),
                     InlineKeyboardButton("📅 Tháng này", callback_data="balance_month")],
                    [InlineKeyboardButton("📅 Năm nay", callback_data="balance_year"),
                     InlineKeyboardButton("📊 Tất cả", callback_data="balance_all")],
                    [InlineKeyboardButton("🔙 Về menu", callback_data="back_to_expense")]
                ]
                
                await safe_edit_message(query, balance_msg, reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            # ===========================================
            # XỬ LÝ CALLBACK KHÔNG XÁC ĐỊNH
            # ===========================================
            
            logger.warning(f"⚠️ Callback không xác định: {data}")
            await safe_edit_message(query, "❌ Chức năng chưa được hỗ trợ!")
            
        except Exception as e:
            logger.error(f"❌ LỖI CALLBACK: {e}", exc_info=True)
            logger.error(f"   • Data gây lỗi: {data}")
            logger.error(f"   • User: {query.from_user.id}")
            
            try:
                await query.edit_message_text(
                    "❌ Có lỗi xảy ra, vui lòng thử lại sau.", 
                    parse_mode=None
                )
            except:
                try:
                    await query.message.reply_text("❌ Có lỗi xảy ra.")
                except:
                    pass
                    
    # ==================== WEBHOOK SETUP ====================
    async def setup_webhook():
        try:
            if not render_config.render_url:
                logger.warning("⚠️ Không có RENDER_EXTERNAL_URL, dùng polling")
                return False
            
            webhook_url = f"{render_config.render_url}/webhook"
            
            await app.bot.delete_webhook(drop_pending_updates=True)
            
            await app.bot.set_webhook(url=webhook_url, allowed_updates=['message', 'callback_query'], drop_pending_updates=True, max_connections=render_config.get_worker_count())
            
            webhook_info = await app.bot.get_webhook_info()
            logger.info(f"✅ Webhook set: {webhook_url}")
            logger.info(f"📊 Pending updates: {webhook_info.pending_update_count}")
            
            return True
        except Exception as e:
            logger.error(f"❌ Lỗi setup webhook: {e}")
            return False

    @webhook_app.route('/webhook', methods=['POST'])
    def webhook():
        try:
            update = Update.de_json(request.get_json(force=True), app.bot)
            asyncio.run_coroutine_threadsafe(app.process_update(update), app.loop)
            return 'OK', 200
        except Exception as e:
            logger.error(f"❌ Webhook error: {e}")
            return 'Error', 500

    @webhook_app.route('/health', methods=['GET'])
    def health():
        try:
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            
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
        port = int(os.environ.get('PORT', 10000))
        logger.info(f"🌐 Starting webhook server on port {port}")
        webhook_app.run(host='0.0.0.0', port=port, threaded=True)

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
        try:
            port = int(os.environ.get('PORT', 10000))
            server = HTTPServer(('0.0.0.0', port), EnhancedHealthHandler)
            logger.info(f"✅ Health server on port {port}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"❌ Health server error: {e}")
            time.sleep(10)

    def smart_startup():
        logger.info("🚀 SMART STARTUP")
        logger.info(f"📊 Render mode: {render_config.is_render}")
        logger.info(f"💾 Memory limit: {render_config.memory_limit}MB")
        logger.info(f"⚙️ CPU limit: {render_config.cpu_limit}")
        logger.info(f"🌐 Render URL: {render_config.render_url}")

        EXPORT_DIR = os.path.join(DATA_DIR, 'exports')
        os.makedirs(EXPORT_DIR, exist_ok=True)
        logger.info(f"📁 Export directory: {EXPORT_DIR}")
        
        test_file = os.path.join(EXPORT_DIR, 'test.txt')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            logger.info("✅ Export directory is writable")
        except Exception as e:
            logger.error(f"❌ Export directory not writable: {e}")
        
        if not init_database():
            logger.error("❌ KHÔNG THỂ KHỞI TẠO DATABASE")
            time.sleep(5)
            
        def fix_database_constraints():
            """Sửa các ràng buộc trong database"""
            conn = None
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                
                # Bật khóa ngoại
                c.execute("PRAGMA foreign_keys = ON")
                logger.info("✅ Đã bật FOREIGN_KEY support")
                
                # Kiểm tra bảng expenses có cột category_id không
                c.execute("PRAGMA table_info(expenses)")
                columns = [col[1] for col in c.fetchall()]
                
                if 'category_id' in columns:
                    logger.info("✅ Bảng expenses có cột category_id")
                else:
                    logger.warning("⚠️ Bảng expenses thiếu cột category_id - Đang thêm...")
                    try:
                        c.execute("ALTER TABLE expenses ADD COLUMN category_id INTEGER")
                        logger.info("✅ Đã thêm cột category_id")
                    except Exception as e:
                        logger.error(f"❌ Không thể thêm cột category_id: {e}")
                
                # Kiểm tra ràng buộc khóa ngoại
                c.execute("PRAGMA foreign_key_list(expenses)")
                fk_list = c.fetchall()
                
                has_fk = False
                for fk in fk_list:
                    if len(fk) >= 5 and fk[3] == 'category_id' and fk[2] == 'expense_categories':
                        has_fk = True
                        logger.info(f"✅ Có ràng buộc khóa ngoại: {fk}")
                        break
                
                if not has_fk:
                    logger.warning("⚠️ Thiếu ràng buộc khóa ngoại giữa expenses và expense_categories")
                    logger.warning("   Có thể gây lỗi khi xóa danh mục")
                
                # Xóa các chi tiêu có category_id không tồn tại
                c.execute('''
                    SELECT COUNT(*) FROM expenses 
                    WHERE category_id IS NOT NULL 
                    AND category_id NOT IN (SELECT id FROM expense_categories)
                ''')
                orphan_count = c.fetchone()[0]
                
                if orphan_count > 0:
                    logger.warning(f"⚠️ Phát hiện {orphan_count} chi tiêu orphan (không có danh mục)")
                    c.execute('''
                        DELETE FROM expenses 
                        WHERE category_id IS NOT NULL 
                        AND category_id NOT IN (SELECT id FROM expense_categories)
                    ''')
                    deleted = c.rowcount
                    logger.info(f"✅ Đã xóa {deleted} chi tiêu orphan")
                
                conn.commit()
                
                # Đếm số lượng để báo cáo
                c.execute("SELECT COUNT(*) FROM expense_categories")
                cat_count = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM expenses")
                exp_count = c.fetchone()[0]
                
                logger.info(f"📊 Thống kê: {cat_count} danh mục, {exp_count} chi tiêu")
                logger.info("✅ Đã kiểm tra và sửa ràng buộc database")
                
            except Exception as e:
                logger.error(f"❌ Lỗi sửa database: {e}")
            finally:
                if conn:
                    conn.close()
            
        # ===== THÊM PHẦN KIỂM TRA TỔNG THỂ VÀO ĐÂY =====
        logger.info("🔍 KIỂM TRA TỔNG THỂ HỆ THỐNG...")
        
        # 1. Migrate dữ liệu admin cũ
        logger.info("🔄 Kiểm tra và migrate dữ liệu admin...")
        try:
            migrate_admin_data()
        except Exception as e:
            logger.error(f"❌ Lỗi migrate admin: {e}")
        
        # 2. Load group owners
        logger.info("🔄 Loading group owners...")
        load_group_owners()
        
        # 3. Kiểm tra dữ liệu trong database
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Đếm số lượng staff trong permissions
            c.execute("SELECT COUNT(*) FROM permissions WHERE role = 'staff'")
            staff_count = c.fetchone()[0]
            
            # Đếm số lượng admin cũ (nếu bảng còn tồn tại)
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='group_admins'")
            old_table_exists = c.fetchone()
            
            old_admin_count = 0
            if old_table_exists:
                c.execute("SELECT COUNT(*) FROM group_admins")
                old_admin_count = c.fetchone()[0]
            
            # Đếm số lượng group owners
            c.execute("SELECT COUNT(*) FROM group_owners")
            group_owners_count = c.fetchone()[0]
            
            # Đếm tổng số user
            c.execute("SELECT COUNT(*) FROM users")
            users_count = c.fetchone()[0]
            
            # Đếm tổng số giao dịch
            c.execute("SELECT COUNT(*) FROM portfolio")
            portfolio_count = c.fetchone()[0]
            
            # Đếm tổng số thu chi
            c.execute("SELECT COUNT(*) FROM incomes")
            income_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM expenses")
            expense_count = c.fetchone()[0]
            
            conn.close()
            
            # In báo cáo kiểm tra
            logger.info("📊 *BÁO CÁO KIỂM TRA DATABASE*")
            logger.info(f"   • Users: {users_count}")
            logger.info(f"   • Group Owners: {group_owners_count}")
            logger.info(f"   • Staff (permissions): {staff_count}")
            if old_table_exists:
                logger.info(f"   • Old admins (group_admins): {old_admin_count}")
            logger.info(f"   • Portfolio transactions: {portfolio_count}")
            logger.info(f"   • Income records: {income_count}")
            logger.info(f"   • Expense records: {expense_count}")
            
            # Cảnh báo nếu còn dữ liệu cũ
            if old_admin_count > 0:
                logger.warning(f"⚠️ Vẫn còn {old_admin_count} admin trong bảng cũ group_admins!")
                logger.warning("   Chạy migrate_admin_data() để chuyển sang bảng mới")
            
            # Kiểm tra consistency
            if staff_count == 0 and old_admin_count > 0:
                logger.warning("⚠️ Có admin cũ nhưng chưa có staff trong permissions!")
                logger.warning("   Đang thử migrate lại...")
                migrate_admin_data()  # Thử migrate lần nữa
                
        except Exception as e:
            logger.error(f"❌ Lỗi kiểm tra database: {e}")
        
        # 4. Kiểm tra cache
        logger.info("🔄 Kiểm tra cache system...")
        logger.info(f"   • Price cache: {price_cache.get_stats()}")
        logger.info(f"   • USDT cache: {usdt_cache.get_stats()}")
        logger.info(f"   • Username cache: {len(username_cache.cache)} entries")
        
        # 5. Kiểm tra thư mục
        logger.info("🔄 Kiểm tra thư mục...")
        db_size = os.path.getsize(DB_PATH) / (1024 * 1024) if os.path.exists(DB_PATH) else 0
        logger.info(f"   • Database size: {db_size:.2f} MB")
        logger.info(f"   • Backup dir: {BACKUP_DIR} ({len(os.listdir(BACKUP_DIR)) if os.path.exists(BACKUP_DIR) else 0} files)")
        logger.info(f"   • Export dir: {EXPORT_DIR} ({len(os.listdir(EXPORT_DIR)) if os.path.exists(EXPORT_DIR) else 0} files)")
        
        # 6. Kiểm tra Render Disk
        if render_config.is_render:
            logger.info("🔄 Kiểm tra Render Disk...")
            if os.path.exists('/data'):
                # Kiểm tra dung lượng
                stat = os.statvfs('/data')
                free_space = stat.f_frsize * stat.f_bavail / (1024 * 1024 * 1024)  # GB
                total_space = stat.f_frsize * stat.f_blocks / (1024 * 1024 * 1024)  # GB
                logger.info(f"   • Render Disk mounted at /data")
                logger.info(f"   • Free space: {free_space:.2f} GB / {total_space:.2f} GB")
                
                # Kiểm tra database có trong disk không
                if DB_PATH.startswith('/data'):
                    logger.info(f"   ✅ Database is on Render Disk: {DB_PATH}")
                else:
                    logger.warning(f"⚠️ Database is NOT on Render Disk: {DB_PATH}")
            else:
                logger.warning("⚠️ Render Disk not mounted at /data")
        
        # ===== KẾT THÚC PHẦN KIỂM TRA =====

        # Các phần còn lại giữ nguyên
        try:
            migrate_database()
        except Exception as e:
            logger.error(f"❌ Lỗi migrate: {e}")
        
        optimize_database()
        
        if render_config.is_render and render_config.render_url:
            logger.info("🌐 Using webhook mode")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(setup_webhook())
            
            threading.Thread(target=run_webhook_server, daemon=True).start()
        else:
            logger.info("🔄 Using polling mode")
            threading.Thread(target=run_health_server, daemon=True).start()
        
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
            app.add_handler(CommandHandler("editthu", edit_income_command))
            app.add_handler(CommandHandler("editchi", edit_expense_command))
            app.add_handler(CommandHandler("suathu", edit_income_command))
            app.add_handler(CommandHandler("suachi", edit_expense_command))
            app.add_handler(CommandHandler("grant", grant_command))
            app.add_handler(CommandHandler("myperm", myperm_command))
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
            os.execv(sys.executable, ['python'] + sys.argv)

except Exception as e:
    logger.critical(f"💥 LỖI NGHIÊM TRỌNG: {e}", exc_info=True)
    time.sleep(10)
    os.execv(sys.executable, ['python'] + sys.argv)
