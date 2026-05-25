from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3, os, json
from analyzer import parse_resume, analyze_resume, calculate_ats_score

app = Flask(__name__)
app.secret_key = 'resume_analyzer_secret_2024'
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_db():
    db = sqlite3.connect('resumes.db')
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS resumes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            text_content TEXT,
            skills TEXT,
            ats_score REAL DEFAULT 0,
            job_description TEXT,
            analysis_data TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    ''')
    db.commit()
    db.close()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def rows_to_dicts(rows):
    """Convert sqlite3.Row objects to plain dicts so Jinja tojson works."""
    return [dict(r) for r in rows]

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = generate_password_hash(request.form['password'])
        db = get_db()
        try:
            db.execute('INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                       (username, email, password))
            db.commit()
            flash('Account created! Please login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username or email already exists.', 'error')
        finally:
            db.close()
    return render_template('auth.html', mode='register')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        flash('Invalid credentials.', 'error')
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    rows = db.execute(
        'SELECT * FROM resumes WHERE user_id = ? ORDER BY uploaded_at DESC',
        (session['user_id'],)
    ).fetchall()
    db.close()
    # Convert to dicts — required for tojson in dashboard.html charts
    resumes = rows_to_dicts(rows)
    scores = [r['ats_score'] for r in resumes if r['ats_score']]
    stats = {
        'total': len(resumes),
        'avg_score': round(sum(scores) / len(scores), 1) if scores else 0,
        'best_score': round(max(scores), 1) if scores else 0,
        'recent': len([s for s in scores if s >= 70])
    }
    return render_template('dashboard.html', resumes=resumes, stats=stats)

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        if 'resume' not in request.files:
            flash('No file selected.', 'error')
            return redirect(request.url)
        file = request.files['resume']
        job_desc = request.form.get('job_description', '').strip()
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            unique_name = f"{session['user_id']}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
            file.save(filepath)
            try:
                text = parse_resume(filepath)
                if not text or text.startswith('Error'):
                    flash('Could not read file. Make sure it is a valid PDF or DOCX.', 'error')
                    return redirect(request.url)
                analysis = analyze_resume(text)
                ats_score = calculate_ats_score(text, job_desc)
                db = get_db()
                db.execute(
                    '''INSERT INTO resumes
                       (user_id, filename, original_name, text_content, skills, ats_score, job_description, analysis_data)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (session['user_id'], unique_name, file.filename, text,
                     json.dumps(analysis['skills']), ats_score, job_desc,
                     json.dumps(analysis))
                )
                resume_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
                db.commit()
                db.close()
                return redirect(url_for('view_resume', resume_id=resume_id))
            except Exception as e:
                flash(f'Error analyzing resume: {str(e)}', 'error')
                return redirect(request.url)
        flash('Invalid file type. Please upload a PDF or DOCX.', 'error')
    return render_template('upload.html')

@app.route('/resume/<int:resume_id>')
def view_resume(resume_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    resume = db.execute(
        'SELECT * FROM resumes WHERE id = ? AND user_id = ?',
        (resume_id, session['user_id'])
    ).fetchone()
    db.close()
    if not resume:
        flash('Resume not found.', 'error')
        return redirect(url_for('dashboard'))
    # Convert to dict for consistent template access
    resume = dict(resume)
    analysis = json.loads(resume['analysis_data']) if resume['analysis_data'] else {}
    skills = json.loads(resume['skills']) if resume['skills'] else {}
    return render_template('result.html', resume=resume, analysis=analysis, skills=skills)

@app.route('/delete/<int:resume_id>', methods=['POST'])
def delete_resume(resume_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    resume = db.execute(
        'SELECT * FROM resumes WHERE id = ? AND user_id = ?',
        (resume_id, session['user_id'])
    ).fetchone()
    if resume:
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], resume['filename']))
        except Exception:
            pass
        db.execute('DELETE FROM resumes WHERE id = ?', (resume_id,))
        db.commit()
    db.close()
    flash('Resume deleted.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/api/scores')
def api_scores():
    if 'user_id' not in session:
        return jsonify([])
    db = get_db()
    resumes = db.execute(
        'SELECT original_name, ats_score, uploaded_at FROM resumes WHERE user_id = ? ORDER BY uploaded_at DESC LIMIT 10',
        (session['user_id'],)
    ).fetchall()
    db.close()
    return jsonify([{'name': r['original_name'][:20], 'score': r['ats_score'], 'date': r['uploaded_at']} for r in resumes])

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
