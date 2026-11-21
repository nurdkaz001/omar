import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

secret_key = os.environ.get('SECRET_KEY')
if not secret_key:
    raise RuntimeError('Не задана переменная окружения SECRET_KEY')

app = Flask(__name__)
app.secret_key = secret_key
DB_NAME = os.environ.get('DB_PATH', 'omar_store.db')


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    with open('schema.sql', 'r', encoding='utf-8') as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def render_page(page_name, **kwargs):
    conn = get_db()
    user = None
    cart_items = []
    cart_count = 0
    total_price = 0
    orders = []

    if 'user_id' in session:
        uid = session['user_id']
        user = conn.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
        cart_items = conn.execute('''
            SELECT c.id AS cart_id, c.size, c.quantity, p.title, p.price, p.image_url
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id = ?
        ''', (uid,)).fetchall()
        cart_count = sum(i['quantity'] for i in cart_items)
        total_price = sum(i['price'] * i['quantity'] for i in cart_items)
        orders = conn.execute('SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC', (uid,)).fetchall()

    conn.close()
    return render_template(
        'index.html',
        page=page_name,
        current_user=user,
        cart_items=cart_items,
        cart_count=cart_count,
        total_price=total_price,
        orders=orders,
        **kwargs
    )


@app.route('/')
def index():
    category = request.args.get('category', 'Все')
    conn = get_db()
    if category == 'Все':
        products = conn.execute('SELECT * FROM products').fetchall()
    else:
        products = conn.execute('SELECT * FROM products WHERE category = ?', (category,)).fetchall()
    conn.close()
    categories = ['Все', 'Верхняя одежда', 'Брюки', 'Трикотаж', 'Обувь']
    return render_page('catalog', products=products, categories=categories, active_category=category)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name'].strip()
        phone = request.form['phone'].strip()
        password = request.form['password']

        conn = get_db()
        if conn.execute('SELECT id FROM users WHERE phone = ?', (phone,)).fetchone():
            conn.close()
            flash('Пользователь с таким номером телефона уже зарегистрирован.', 'error')
            return redirect(url_for('register'))

        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO users (name, phone, password_hash, created_at) VALUES (?, ?, ?, ?)',
            (name, phone, generate_password_hash(password), datetime.now().strftime('%d.%m.%Y %H:%M'))
        )
        conn.commit()
        session['user_id'] = cursor.lastrowid
        conn.close()
        flash('Добро пожаловать в OMAR ASTANA!', 'success')
        return redirect(url_for('profile'))

    return render_page('register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form['phone'].strip()
        password = request.form['password']

        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE phone = ?', (phone,)).fetchone()
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

    size = request.form.get('size', 'Стандарт')
    uid = session['user_id']
    conn = get_db()
    existing = conn.execute(
        'SELECT * FROM cart WHERE user_id = ? AND product_id = ? AND size = ?',
        (uid, product_id, size)
    ).fetchone()

    if existing:
        conn.execute('UPDATE cart SET quantity = quantity + 1 WHERE id = ?', (existing['id'],))
    else:
        conn.execute(
            'INSERT INTO cart (user_id, product_id, size, quantity) VALUES (?, ?, ?, 1)',
            (uid, product_id, size)
        )
    conn.commit()
    conn.close()
    flash('Товар добавлен в корзину.', 'success')
    return redirect(request.referrer or url_for('index'))


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
    item = conn.execute('SELECT * FROM cart WHERE id = ? AND user_id = ?', (cart_id, session['user_id'])).fetchone()
    if item:
        if action == 'plus':
            conn.execute('UPDATE cart SET quantity = quantity + 1 WHERE id = ?', (cart_id,))
        elif action == 'minus':
            if item['quantity'] > 1:
                conn.execute('UPDATE cart SET quantity = quantity - 1 WHERE id = ?', (cart_id,))
            else:
                conn.execute('DELETE FROM cart WHERE id = ?', (cart_id,))
        elif action == 'delete':
            conn.execute('DELETE FROM cart WHERE id = ?', (cart_id,))
        conn.commit()
    conn.close()
    return redirect(url_for('cart'))


@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        conn = get_db()
        uid = session['user_id']
        items = conn.execute('''
            SELECT c.size, c.quantity, p.title, p.price
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id = ?
        ''', (uid,)).fetchall()

        if not items:
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

        conn.execute('''
            INSERT INTO orders (user_id, total_amount, delivery_method, address_or_branch, payment_method, status, items_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (uid, total_price, delivery_method, location, payment_method, status, items_summary, datetime.now().strftime('%d.%m.%Y %H:%M')))

        conn.execute('DELETE FROM cart WHERE user_id = ?', (uid,))
        conn.commit()
        conn.close()

        flash('Заказ успешно оплачен и оформлен!', 'success')
        return redirect(url_for('profile'))

    return render_page('checkout')


@app.route('/profile')
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_page('profile')


# Инициализация БД при старте (в том числе при запуске через gunicorn на Render)
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
