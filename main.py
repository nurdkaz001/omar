import os
import uuid
import html
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import requests
from psycopg2.extras import RealDictCursor

load_dotenv()

secret_key = os.environ.get('SECRET_KEY')
if not secret_key:
    raise RuntimeError('Не задана переменная окружения SECRET_KEY')

database_url = os.environ.get('DATABASE_URL')
if not database_url:
    raise RuntimeError('Не задана переменная окружения DATABASE_URL')

# Опциональные переменные Supabase, если понадобятся в будущем (Storage / Auth / API)
supabase_url = os.environ.get('SUPABASE_URL', '')
supabase_key = os.environ.get('SUPABASE_KEY', '')

# Telegram-бот для уведомлений о заказах (необязательно)
telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')

app = Flask(__name__)
app.secret_key = secret_key
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # общий лимит загрузки, 32 МБ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_db():
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    with open(os.path.join(BASE_DIR, 'schema.sql'), 'r', encoding='utf-8') as f:
        cur.execute(f.read())
    conn.commit()
    cur.close()
    conn.close()


def send_telegram(text):
    """Отправляет сообщение в Telegram. Ошибки не ломают сайт: просто пишутся в лог."""
    if not telegram_token or not telegram_chat_id:
        return
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{telegram_token}/sendMessage',
            json={
                'chat_id': telegram_chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            },
            timeout=5,
        )
        if resp.status_code != 200:
            print('Telegram не принял сообщение:', resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        print('Не удалось отправить в Telegram:', e)


def split_list(value):
    """'a, b,c' -> ['a', 'b', 'c']"""
    return [x.strip() for x in (value or '').split(',') if x.strip()]


def render_page(page_name, **kwargs):
    conn = get_db()
    cur = conn.cursor()
    user = None
    cart_items = []
    cart_count = 0
    total_price = 0
    orders = []

    if 'user_id' in session:
        uid = session['user_id']
        cur.execute('SELECT * FROM users WHERE id = %s', (uid,))
        user = cur.fetchone()

        cur.execute('''
            SELECT c.id AS cart_id, c.size, c.quantity, p.title, p.price, p.image_url
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id = %s
            ORDER BY c.id ASC
        ''', (uid,))
        cart_items = cur.fetchall()

        cart_count = sum(i['quantity'] for i in cart_items)
        total_price = sum(i['price'] * i['quantity'] for i in cart_items)

        cur.execute('SELECT * FROM orders WHERE user_id = %s ORDER BY id DESC', (uid,))
        orders = cur.fetchall()

    # Общие данные для шапки, подвала и оформления заказа
    cur.execute('SELECT * FROM stores ORDER BY id ASC')
    stores = cur.fetchall()

    cur.execute('SELECT category FROM products GROUP BY category ORDER BY MIN(id)')
    nav_categories = [r['category'] for r in cur.fetchall()]

    cur.close()
    conn.close()

    return render_template(
        'index.html',
        page=page_name,
        current_user=user,
        cart_items=cart_items,
        cart_count=cart_count,
        total_price=total_price,
        orders=orders,
        stores=stores,
        nav_categories=nav_categories,
        **kwargs
    )


# ---------- Главная ----------
@app.route('/')
def index():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT category, COUNT(*) AS cnt, MIN(image_url) AS image_url
        FROM products
        GROUP BY category
        ORDER BY MIN(id)
    ''')
    categories = cur.fetchall()
    cur.execute('SELECT * FROM products WHERE is_new = TRUE ORDER BY id DESC LIMIT 8')
    new_products = cur.fetchall()
    cur.execute('SELECT * FROM products WHERE is_sale = TRUE ORDER BY id DESC LIMIT 8')
    sale_products = cur.fetchall()
    cur.close()
    conn.close()
    return render_page('home', categories=categories, new_products=new_products, sale_products=sale_products)


# ---------- Каталог: разделы, поиск, сортировка ----------
@app.route('/catalog')
def catalog():
    category = request.args.get('category', 'Все')
    sort = request.args.get('sort', 'new')
    q = request.args.get('q', '').strip()
    flag = request.args.get('flag', '')
    if flag not in ('new', 'sale'):
        flag = ''

    order_by = {
        'new': 'id DESC',
        'cheap': 'price ASC',
        'expensive': 'price DESC',
    }.get(sort, 'id DESC')

    where, params = [], []
    if category != 'Все':
        where.append('category = %s')
        params.append(category)
    if flag == 'new':
        where.append('is_new = TRUE')
    elif flag == 'sale':
        where.append('is_sale = TRUE')
    if q:
        where.append('title ILIKE %s')
        params.append(f'%{q}%')

    sql = 'SELECT * FROM products'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += f' ORDER BY {order_by}'

    conn = get_db()
    cur = conn.cursor()
    cur.execute(sql, params)
    products = cur.fetchall()
    cur.close()
    conn.close()

    return render_page('catalog', products=products, active_category=category, sort=sort, q=q, flag=flag)


# ---------- Страница товара ----------
@app.route('/product/<int:product_id>')
def product(product_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s', (product_id,))
    item = cur.fetchone()
    if not item:
        cur.close()
        conn.close()
        abort(404)

    cur.execute('SELECT store_id, size, qty FROM stock WHERE product_id = %s', (product_id,))
    stock = {}
    for r in cur.fetchall():
        stock.setdefault(r['size'], {})[r['store_id']] = r['qty']

    cur.execute(
        'SELECT * FROM products WHERE category = %s AND id <> %s ORDER BY id DESC LIMIT 4',
        (item['category'], product_id)
    )
    similar = cur.fetchall()
    cur.close()
    conn.close()

    images = [u for u in [item['image_url']] + split_list(item['gallery']) if u]
    sizes = split_list(item['sizes'])

    return render_page('product', product=item, sizes=sizes, stock=stock, images=images, similar=similar)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name'].strip()
        phone = request.form['phone'].strip()
        password = request.form['password']

        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE phone = %s', (phone,))
        if cur.fetchone():
            cur.close()
            conn.close()
            flash('Пользователь с таким номером телефона уже зарегистрирован.', 'error')
            return redirect(url_for('register'))

        cur.execute(
            '''
            INSERT INTO users (name, phone, password_hash, created_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            ''',
            (name, phone, generate_password_hash(password), datetime.now().strftime('%d.%m.%Y %H:%M'))
        )
        new_user_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        conn.close()

        session['user_id'] = new_user_id
        flash("Добро пожаловать в MEN'S REPUBLIC!", 'success')
        return redirect(url_for('profile'))

    return render_page('register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form['phone'].strip()
        password = request.form['password']

        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE phone = %s', (phone,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            flash('Вы успешно вошли в аккаунт.', 'success')
            return redirect(url_for('profile'))
        flash('Неверный телефон или пароль.', 'error')

    return render_page('login')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash('Вы вышли из аккаунта.', 'info')
    return redirect(url_for('index'))


@app.route('/add_to_cart/<int:product_id>', methods=['POST'])
def add_to_cart(product_id):
    if 'user_id' not in session:
        flash('Сначала войдите или зарегистрируйтесь.', 'error')
        return redirect(url_for('login'))

    size = request.form.get('size', '').strip()
    uid = session['user_id']

    conn = get_db()
    cur = conn.cursor()

    cur.execute('SELECT sizes FROM products WHERE id = %s', (product_id,))
    prod = cur.fetchone()
    if not prod:
        cur.close()
        conn.close()
        abort(404)

    if size not in split_list(prod['sizes']):
        cur.close()
        conn.close()
        flash('Выберите размер.', 'error')
        return redirect(url_for('product', product_id=product_id))

    # Если остатки ведутся и этого размера нигде нет, в корзину не добавляем
    cur.execute(
        'SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS total FROM stock WHERE product_id = %s AND size = %s',
        (product_id, size)
    )
    st = cur.fetchone()
    if st['n'] > 0 and st['total'] == 0:
        cur.close()
        conn.close()
        flash('Этого размера сейчас нет в наличии.', 'error')
        return redirect(url_for('product', product_id=product_id))

    cur.execute(
        'SELECT * FROM cart WHERE user_id = %s AND product_id = %s AND size = %s',
        (uid, product_id, size)
    )
    existing = cur.fetchone()

    if existing:
        cur.execute('UPDATE cart SET quantity = quantity + 1 WHERE id = %s', (existing['id'],))
    else:
        cur.execute(
            'INSERT INTO cart (user_id, product_id, size, quantity) VALUES (%s, %s, %s, 1)',
            (uid, product_id, size)
        )
    conn.commit()
    cur.close()
    conn.close()

    flash('Товар добавлен в корзину.', 'success')
    return redirect(url_for('product', product_id=product_id))


@app.route('/cart')
def cart():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_page('cart')


@app.route('/cart/update/<int:cart_id>/<action>')
def update_cart(cart_id, action):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM cart WHERE id = %s AND user_id = %s', (cart_id, session['user_id']))
    item = cur.fetchone()

    if item:
        if action == 'plus':
            cur.execute('UPDATE cart SET quantity = quantity + 1 WHERE id = %s', (cart_id,))
        elif action == 'minus':
            if item['quantity'] > 1:
                cur.execute('UPDATE cart SET quantity = quantity - 1 WHERE id = %s', (cart_id,))
            else:
                cur.execute('DELETE FROM cart WHERE id = %s', (cart_id,))
        elif action == 'delete':
            cur.execute('DELETE FROM cart WHERE id = %s', (cart_id,))
        conn.commit()

    cur.close()
    conn.close()
    return redirect(url_for('cart'))


@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        conn = get_db()
        cur = conn.cursor()
        uid = session['user_id']

        cur.execute('''
            SELECT c.size, c.quantity, p.title, p.price
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id = %s
        ''', (uid,))
        items = cur.fetchall()

        if not items:
            cur.close()
            conn.close()
            return redirect(url_for('index'))

        total_price = sum(i['price'] * i['quantity'] for i in items)
        delivery_method = request.form['delivery_method']
        branch = request.form.get('branch', '')
        address = request.form.get('address', '').strip()
        payment_method = request.form['payment_method']

        location = branch if delivery_method == 'Самовывоз из филиала' else f"Доставка: {address}"
        items_summary = ', '.join([f"{i['title']} (размер {i['size']}) x{i['quantity']}" for i in items])
        status = 'Оплачен онлайн' if payment_method != 'При получении' else 'Ожидает оплаты при получении'

        cur.execute('''
            INSERT INTO orders (user_id, total_amount, delivery_method, address_or_branch, payment_method, status, items_summary, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (uid, total_price, delivery_method, location, payment_method, status, items_summary, datetime.now().strftime('%d.%m.%Y %H:%M')))
        order_id = cur.fetchone()['id']

        cur.execute('SELECT name, phone FROM users WHERE id = %s', (uid,))
        buyer = cur.fetchone() or {}

        cur.execute('DELETE FROM cart WHERE user_id = %s', (uid,))
        conn.commit()
        cur.close()
        conn.close()

        esc = html.escape
        total_text = f'{total_price:,}'.replace(',', ' ')
        lines = '\n'.join(
            f"• {esc(i['title'])}, размер {esc(str(i['size']))} × {i['quantity']}" for i in items
        )
        send_telegram(
            f"🛍 <b>Новый заказ №{order_id}</b>\n\n"
            f"{lines}\n\n"
            f"💰 <b>{total_text} ₸</b>\n"
            f"👤 {esc(buyer.get('name', ''))}, {esc(buyer.get('phone', ''))}\n"
            f"📦 {esc(delivery_method)}: {esc(location)}\n"
            f"💳 {esc(payment_method)}"
        )

        flash('Заказ успешно оплачен и оформлен!', 'success')
        return redirect(url_for('profile'))

    return render_page('checkout')


@app.route('/profile')
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_page('profile')



# =====================================================================
#                           АДМИН-ПАНЕЛЬ
# =====================================================================
ORDER_STATUSES = [
    'Оплачен онлайн',
    'Ожидает оплаты при получении',
    'Собран',
    'Передан курьеру',
    'Выдан',
    'Отменён',
]
NEW_STATUSES = ('Оплачен онлайн', 'Ожидает оплаты при получении')
LOW_LIMIT = 3  # «мало»: столько штук размера и меньше по всем магазинам вместе


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash('Войдите в аккаунт администратора.', 'error')
            return redirect(url_for('login'))
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT is_admin FROM users WHERE id = %s', (session['user_id'],))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row or not row['is_admin']:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def admin_page(section, **kwargs):
    """Общие данные шапки админки + рендер admin.html"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) AS n FROM orders WHERE status = ANY(%s)', (list(NEW_STATUSES),))
    new_orders = cur.fetchone()['n']
    cur.execute(
        'SELECT COUNT(*) AS n FROM ('
        ' SELECT product_id, size FROM stock GROUP BY product_id, size HAVING SUM(qty) <= %s'
        ') t',
        (LOW_LIMIT,)
    )
    low_count = cur.fetchone()['n']
    cur.close()
    conn.close()
    return render_template('admin.html', section=section, new_orders=new_orders,
                           low_count=low_count, low_limit=LOW_LIMIT, statuses=ORDER_STATUSES, **kwargs)


def to_int(value, default=0, minimum=0):
    try:
        return max(minimum, int(str(value).replace(' ', '').replace('\xa0', '').strip()))
    except (ValueError, TypeError):
        return default


@app.route('/admin')
@admin_required
def admin_home():
    return redirect(url_for('admin_orders'))


# ---------- Заказы ----------
@app.route('/admin/orders')
@admin_required
def admin_orders():
    status = request.args.get('status', '').strip()
    conn = get_db()
    cur = conn.cursor()

    sql = (
        'SELECT o.*, u.name AS user_name, u.phone AS user_phone '
        'FROM orders o JOIN users u ON u.id = o.user_id'
    )
    params = []
    if status in ORDER_STATUSES:
        sql += ' WHERE o.status = %s'
        params.append(status)
    else:
        status = ''
    sql += ' ORDER BY o.id DESC'
    cur.execute(sql, params)
    orders = cur.fetchall()

    cur.execute(
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(total_amount) FILTER (WHERE status <> 'Отменён'), 0) AS rev FROM orders"
    )
    agg = cur.fetchone()
    cur.close()
    conn.close()

    return admin_page('orders', orders=orders, active_status=status,
                      total_orders=agg['n'], revenue=int(agg['rev']))


@app.route('/admin/orders/<int:order_id>/status', methods=['POST'])
@admin_required
def admin_order_status(order_id):
    status = request.form.get('status', '')
    if status not in ORDER_STATUSES:
        abort(400)
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE orders SET status = %s WHERE id = %s', (status, order_id))
    conn.commit()
    cur.close()
    conn.close()
    flash(f'Заказ №{order_id}: статус «{status}».', 'success')
    return redirect(request.referrer or url_for('admin_orders'))


@app.route('/admin/orders/<int:order_id>/delete', methods=['POST'])
@admin_required
def admin_order_delete(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM orders WHERE id = %s', (order_id,))
    conn.commit()
    cur.close()
    conn.close()
    flash(f'Заказ №{order_id} удалён.', 'info')
    return redirect(request.referrer or url_for('admin_orders'))


# ---------- Товары ----------
@app.route('/admin/products')
@admin_required
def admin_products():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT p.*, '
        '  COALESCE((SELECT SUM(qty) FROM stock s WHERE s.product_id = p.id), 0) AS total, '
        '  EXISTS (SELECT 1 FROM stock s WHERE s.product_id = p.id '
        '          GROUP BY s.size HAVING SUM(s.qty) <= %s) AS low_sizes '
        'FROM products p ORDER BY p.id DESC',
        (LOW_LIMIT,)
    )
    products = cur.fetchall()
    cur.close()
    conn.close()
    return admin_page('products', products=products)


@app.route('/admin/low-stock')
@admin_required
def admin_low_stock():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT p.id, p.title, p.category, p.image_url, s.size, SUM(s.qty) AS total '
        'FROM stock s JOIN products p ON p.id = s.product_id '
        'GROUP BY p.id, p.title, p.category, p.image_url, s.size '
        'HAVING SUM(s.qty) <= %s '
        'ORDER BY SUM(s.qty) ASC, p.title ASC',
        (LOW_LIMIT,)
    )
    items = cur.fetchall()
    cur.close()
    conn.close()
    return admin_page('low', low_items=items)


# ---------- Загрузка фото ----------
MIME_BY_EXT = {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}
SUPABASE_BUCKET = os.environ.get('SUPABASE_BUCKET', 'products')
UPLOAD_DIR = os.path.join(BASE_DIR, 'static', 'uploads')


def sniff_ext(data):
    """Тип файла определяем по содержимому, а не по имени."""
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:4] == b'GIF8':
        return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return None


def ensure_video_column():
    """Один раз добавляет в products колонку для видео, если её ещё нет."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS video_url TEXT DEFAULT ''")
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS is_new BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS is_sale BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS old_price INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print('Не удалось добавить колонку video_url:', e)


ensure_video_column()


def _store_file(data, name, mime):
    """Кладёт файл в Supabase Storage (или в static/uploads) и возвращает ссылку."""
    if supabase_url and supabase_key:
        base = supabase_url.rstrip('/')
        resp = requests.post(
            f'{base}/storage/v1/object/{SUPABASE_BUCKET}/{name}',
            headers={
                'Authorization': f'Bearer {supabase_key}',
                'apikey': supabase_key,
                'Content-Type': mime,
                'x-upsert': 'true',
            },
            data=data,
            timeout=120,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f'Хранилище не приняло файл (код {resp.status_code}): {resp.text[:200]}. '
                f'Проверьте бакет «{SUPABASE_BUCKET}»: разрешённые типы файлов, лимит размера и ключ SUPABASE_KEY.'
            )
        return f'{base}/storage/v1/object/public/{SUPABASE_BUCKET}/{name}'

    # Запасной вариант без Supabase Storage: папка static/uploads
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, name), 'wb') as f:
        f.write(data)
    return url_for('static', filename=f'uploads/{name}')


def save_video(file_storage):
    """Сохраняет видео товара (MP4 или WebM). Пустое поле -> None."""
    if not file_storage or not file_storage.filename:
        return None
    data = file_storage.read()
    if not data:
        return None
    if data[4:8] == b'ftyp':
        ext, mime = 'mp4', 'video/mp4'
    elif data[:4] == b'\x1aE\xdf\xa3':
        ext, mime = 'webm', 'video/webm'
    else:
        raise ValueError(f'Файл «{file_storage.filename}» не похож на видео. Нужен MP4 или WebM.')
    return _store_file(data, f'{uuid.uuid4().hex}.{ext}', mime)


def save_image(file_storage):
    """Сохраняет фото и возвращает ссылку на него. Пустое поле -> None."""
    if not file_storage or not file_storage.filename:
        return None
    data = file_storage.read()
    if not data:
        return None
    ext = sniff_ext(data)
    if not ext:
        raise ValueError(f'Файл «{file_storage.filename}» не похож на фото. Нужен JPG, PNG, WebP или GIF.')
    return _store_file(data, f'{uuid.uuid4().hex}.{ext}', MIME_BY_EXT[ext])


def read_product_form():
    f = request.form
    return {
        'title': f.get('title', '').strip(),
        'category': f.get('category', '').strip(),
        'price': to_int(f.get('price')),
        'sizes': ','.join(split_list(f.get('sizes'))),
        'description': f.get('description', '').strip(),
        'image_url': f.get('image_url', '').strip(),
        'gallery': '',
        'video_url': f.get('video_url', '').strip(),
        'is_new': bool(f.get('is_new')),
        'is_sale': bool(f.get('is_sale')),
        'old_price': to_int(f.get('old_price')),
    }


def check_sale(d):
    """Если товар со скидкой и указана старая цена, она должна быть выше текущей."""
    if d['is_sale'] and d['old_price'] and d['old_price'] <= d['price']:
        return 'Старая цена должна быть больше текущей (или оставьте поле пустым).'
    return None


def collect_photos(d):
    """Добавляет в d загруженные и вставленные по ссылке фото. Бросает ValueError/RuntimeError."""
    main = save_image(request.files.get('image_file'))
    if main:
        d['image_url'] = main

    gallery = request.form.getlist('gallery_keep') + split_list(request.form.get('gallery_urls'))
    for fs in request.files.getlist('gallery_files'):
        url = save_image(fs)
        if url:
            gallery.append(url)
    d['gallery'] = ','.join(gallery)

    video = save_video(request.files.get('video_file'))
    if video:
        d['video_url'] = video
    elif request.form.get('video_remove'):
        d['video_url'] = ''

    if not d['image_url']:
        raise ValueError('Добавьте главное фото товара.')


def save_stock(cur, product_id, sizes):
    """Остатки из полей qty__<размер>__<id магазина>. Размеры, которых больше нет, удаляются."""
    cur.execute('SELECT id FROM stores')
    store_ids = [r['id'] for r in cur.fetchall()]
    cur.execute('DELETE FROM stock WHERE product_id = %s AND NOT (size = ANY(%s))', (product_id, sizes))
    for size in sizes:
        for sid in store_ids:
            qty = to_int(request.form.get(f'qty__{size}__{sid}'), 0)
            cur.execute(
                'INSERT INTO stock (product_id, store_id, size, qty) VALUES (%s, %s, %s, %s) '
                'ON CONFLICT (product_id, store_id, size) DO UPDATE SET qty = EXCLUDED.qty',
                (product_id, sid, size, qty)
            )


def stock_from_form():
    """Введённые остатки из формы (чтобы не потерять их, если сохранение не удалось)."""
    stock = {}
    for key, value in request.form.items():
        if key.startswith('qty__'):
            try:
                _, size, sid = key.split('__')
                stock.setdefault(size, {})[int(sid)] = to_int(value, 0)
            except ValueError:
                continue
    return stock


def product_form_context(product=None, is_edit=False, stock=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM stores ORDER BY id')
    stores = cur.fetchall()
    cur.execute('SELECT DISTINCT category FROM products ORDER BY category')
    categories = [r['category'] for r in cur.fetchall()]
    if stock is None:
        stock = {}
        if product and is_edit:
            cur.execute('SELECT store_id, size, qty FROM stock WHERE product_id = %s', (product['id'],))
            for r in cur.fetchall():
                stock.setdefault(r['size'], {})[r['store_id']] = r['qty']
    cur.close()
    conn.close()
    gallery_list = split_list(product['gallery']) if product and product.get('gallery') else []
    return dict(product=product, is_edit=is_edit, stores=stores, categories=categories,
                stock=stock, gallery_list=gallery_list)


def form_error(message, d, product_id=None):
    """Показывает форму снова с уже введёнными данными и сообщением об ошибке."""
    flash(message, 'error')
    product = dict(d)
    if product_id:
        product['id'] = product_id
    # Ранее загруженные фото остаются в форме
    product['gallery'] = ','.join(request.form.getlist('gallery_keep'))
    return admin_page('form', **product_form_context(product, is_edit=bool(product_id), stock=stock_from_form()))


@app.route('/admin/products/new', methods=['GET', 'POST'])
@admin_required
def admin_product_new():
    if request.method == 'POST':
        d = read_product_form()
        sizes = split_list(d['sizes'])
        if not d['title'] or not d['category'] or not sizes:
            return form_error('Заполните название, раздел и размеры.', d)
        if check_sale(d):
            return form_error(check_sale(d), d)
        try:
            collect_photos(d)
        except (ValueError, RuntimeError, requests.RequestException) as e:
            return form_error(str(e), d)

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO products (title, category, price, sizes, description, image_url, gallery, video_url, is_new, is_sale, old_price) '
            'VALUES (%(title)s, %(category)s, %(price)s, %(sizes)s, %(description)s, %(image_url)s, %(gallery)s, %(video_url)s, '
            '%(is_new)s, %(is_sale)s, %(old_price)s) '
            'RETURNING id',
            d
        )
        pid = cur.fetchone()['id']
        save_stock(cur, pid, sizes)
        conn.commit()
        cur.close()
        conn.close()
        flash('Товар добавлен.', 'success')
        return redirect(url_for('admin_products'))
    return admin_page('form', **product_form_context())


@app.route('/admin/products/<int:product_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_product_edit(product_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s', (product_id,))
    item = cur.fetchone()
    if not item:
        cur.close()
        conn.close()
        abort(404)

    if request.method == 'POST':
        d = read_product_form()
        sizes = split_list(d['sizes'])
        if not d['title'] or not d['category'] or not sizes:
            cur.close()
            conn.close()
            return form_error('Заполните название, раздел и размеры.', d, product_id)
        if check_sale(d):
            cur.close()
            conn.close()
            return form_error(check_sale(d), d, product_id)
        try:
            collect_photos(d)
        except (ValueError, RuntimeError, requests.RequestException) as e:
            cur.close()
            conn.close()
            return form_error(str(e), d, product_id)

        d['id'] = product_id
        cur.execute(
            'UPDATE products SET title=%(title)s, category=%(category)s, price=%(price)s, sizes=%(sizes)s, '
            'description=%(description)s, image_url=%(image_url)s, gallery=%(gallery)s, video_url=%(video_url)s, '
            'is_new=%(is_new)s, is_sale=%(is_sale)s, old_price=%(old_price)s WHERE id=%(id)s',
            d
        )
        save_stock(cur, product_id, sizes)
        conn.commit()
        cur.close()
        conn.close()
        flash('Изменения сохранены.', 'success')
        return redirect(url_for('admin_products'))

    cur.close()
    conn.close()
    return admin_page('form', **product_form_context(item, is_edit=True))


@app.route('/admin/products/<int:product_id>/flag/<kind>', methods=['POST'])
@admin_required
def admin_product_flag(product_id, kind):
    column = {'new': 'is_new', 'sale': 'is_sale'}.get(kind)
    if not column:
        abort(400)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f'UPDATE products SET {column} = NOT {column} WHERE id = %s RETURNING {column} AS val, title', (product_id,))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        abort(404)
    label = 'Новинка' if kind == 'new' else 'Скидка'
    flash(f'«{row["title"]}»: {label} {"включена" if row["val"] else "выключена"}.', 'success')
    return redirect(request.referrer or url_for('admin_products'))


@app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
@admin_required
def admin_product_delete(product_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM products WHERE id = %s', (product_id,))
    conn.commit()
    cur.close()
    conn.close()
    flash('Товар удалён.', 'info')
    return redirect(url_for('admin_products'))


@app.errorhandler(413)
def too_large(_e):
    flash('Файлы слишком большие. Загрузите фото поменьше (до 32 МБ суммарно).', 'error')
    return redirect(request.referrer or url_for('admin_products'))


@app.errorhandler(403)
def forbidden(_e):
    flash('Нет доступа к админ-панели.', 'error')
    return redirect(url_for('index'))


try:
    init_db()
except Exception as e:
    print(f'Предупреждение при инициализации БД: {e}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
