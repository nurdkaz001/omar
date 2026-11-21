import os
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
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

app = Flask(__name__)
app.secret_key = secret_key
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

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
    cur.execute('SELECT * FROM products ORDER BY id DESC LIMIT 8')
    new_products = cur.fetchall()
    cur.close()
    conn.close()
    return render_page('home', categories=categories, new_products=new_products)


# ---------- Каталог: разделы, поиск, сортировка ----------
@app.route('/catalog')
def catalog():
    category = request.args.get('category', 'Все')
    sort = request.args.get('sort', 'new')
    q = request.args.get('q', '').strip()

    order_by = {
        'new': 'id DESC',
        'cheap': 'price ASC',
        'expensive': 'price DESC',
    }.get(sort, 'id DESC')

    where, params = [], []
    if category != 'Все':
        where.append('category = %s')
        params.append(category)
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

    return render_page('catalog', products=products, active_category=category, sort=sort, q=q)


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
        flash('Добро пожаловать в OMAR ASTANA!', 'success')
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

        location = branch if delivery_method == 'Самовывоз из филиала' else f"Доставка по Астане: {address}"
        items_summary = ', '.join([f"{i['title']} (размер {i['size']}) x{i['quantity']}" for i in items])
        status = 'Оплачен онлайн' if payment_method != 'При получении' else 'Ожидает оплаты при получении'

        cur.execute('''
            INSERT INTO orders (user_id, total_amount, delivery_method, address_or_branch, payment_method, status, items_summary, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (uid, total_price, delivery_method, location, payment_method, status, items_summary, datetime.now().strftime('%d.%m.%Y %H:%M')))

        cur.execute('DELETE FROM cart WHERE user_id = %s', (uid,))
        conn.commit()
        cur.close()
        conn.close()

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
        return max(minimum, int(str(value).strip()))
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


def read_product_form():
    f = request.form
    return {
        'title': f.get('title', '').strip(),
        'category': f.get('category', '').strip(),
        'price': to_int(f.get('price')),
        'sizes': ','.join(split_list(f.get('sizes'))),
        'description': f.get('description', '').strip(),
        'image_url': f.get('image_url', '').strip(),
        'gallery': ','.join(split_list(f.get('gallery'))),
    }


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


def product_form_context(product=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM stores ORDER BY id')
    stores = cur.fetchall()
    cur.execute('SELECT DISTINCT category FROM products ORDER BY category')
    categories = [r['category'] for r in cur.fetchall()]
    stock = {}
    if product:
        cur.execute('SELECT store_id, size, qty FROM stock WHERE product_id = %s', (product['id'],))
        for r in cur.fetchall():
            stock.setdefault(r['size'], {})[r['store_id']] = r['qty']
    cur.close()
    conn.close()
    return dict(product=product, stores=stores, categories=categories, stock=stock)


@app.route('/admin/products/new', methods=['GET', 'POST'])
@admin_required
def admin_product_new():
    if request.method == 'POST':
        d = read_product_form()
        sizes = split_list(d['sizes'])
        if not d['title'] or not d['category'] or not sizes:
            flash('Заполните название, раздел и размеры.', 'error')
            return redirect(url_for('admin_product_new'))
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO products (title, category, price, sizes, description, image_url, gallery) '
            'VALUES (%(title)s, %(category)s, %(price)s, %(sizes)s, %(description)s, %(image_url)s, %(gallery)s) '
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
            flash('Заполните название, раздел и размеры.', 'error')
            return redirect(url_for('admin_product_edit', product_id=product_id))
        d['id'] = product_id
        cur.execute(
            'UPDATE products SET title=%(title)s, category=%(category)s, price=%(price)s, sizes=%(sizes)s, '
            'description=%(description)s, image_url=%(image_url)s, gallery=%(gallery)s WHERE id=%(id)s',
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
    return admin_page('form', **product_form_context(item))


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
