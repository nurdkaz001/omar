import os
from datetime import datetime
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


try:
    init_db()
except Exception as e:
    print(f'Предупреждение при инициализации БД: {e}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
