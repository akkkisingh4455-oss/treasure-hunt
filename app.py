import os, json, sqlite3, secrets, random, math
from functools import wraps
from flask import Flask, request, redirect, session, g, abort, render_template_string as R

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hunt.db")
NOW = "datetime('now','localtime')"

# Kis step (checkpoint number) par puzzle ki jagah GAME aayega. Pehle step (start) par koi game nahi.
# Options: memory, cipher, sliding, lights, sudoku, hanoi. Badalna ho to yahin badlein, jaise {2: "cipher", 4: "sliding"}
# Games ab admin se tay hote hain: Admin -> "🎮 Games". Yahan sirf default (auto) hai, jab tak admin kuch save na kare.
GAME_KINDS = ["memory", "cipher", "sliding", "lights", "sudoku", "hanoi"]
LEVELS = ["easy", "medium", "hard"]
GAME_ORDER = ["memory", "cipher", "sliding", "lights"]
AUTO_LEVEL = {"memory": "easy", "cipher": "medium", "sliding": "medium", "lights": "hard"}

def game_steps(k):
    """k = team ki normal locations ki ginti. Return {step: (game, level)}."""
    r = q("select v from settings where k='games_cfg'", one=True)
    if r:                                         # admin ne khud tay kiya hai
        try: cfg = json.loads(r["v"])
        except Exception: cfg = {}
        return {int(p): (v[0], v[1] if v[1] in LEVELS else "medium") for p, v in cfg.items() if int(p) <= k and v[0] in GAME_KINDS}
    pos, prev = [], 0                             # auto: 4 games aasaan se kathin, route par barabar felte hain
    for f in (0.3, 0.5, 0.7, 0.9):
        p = max(round(k * f), prev + 1, 1)
        if p > k: break
        pos.append(p); prev = p
    kinds = GAME_ORDER[len(GAME_ORDER) - len(pos):]
    return {p: (g, AUTO_LEVEL[g]) for p, g in zip(pos, kinds)}

SCHEMA = """
create table if not exists teams(id integer primary key, name text unique, code text);
create table if not exists locations(id integer primary key, name text, slug text unique, kind text default 'normal');
create table if not exists riddles(location_id int, variant int, clue text default '', puzzle text default '', code text default '', primary key(location_id,variant));
create table if not exists steps(team_id int, pos int, location_id int, scan_ts text, done_ts text, variant int default 0, primary key(team_id,pos));
create table if not exists games(team_id int, pos int, kind text, state text, primary key(team_id,pos));
create table if not exists settings(k text primary key, v text);"""

def per_team(conn, n):
    """Har team kitni normal locations par jayegi (admin se badalta hai). 0/ghalat = sab."""
    r = conn.execute("select v from settings where k='per_team'").fetchone()
    try: k = int(r[0]) if r else 0
    except Exception: k = 0
    return n if k <= 0 else min(k, n)

def gen_routes(conn, only=None):
    """Har team ko alag order. 10 locations par 20 teams: har 10 teams ka alag 'kadam' (1,3,7,9),
    taaki ek jagah par pahunchi do teams ka agla location alag ho. variant = team ka group (clue/puzzle/code ka set)."""
    teams = [r[0] for r in conn.execute("select id from teams order by id")]
    locs = [r[0] for r in conn.execute("select id from locations where kind='normal' order by id")]
    fin = [r[0] for r in conn.execute("select id from locations where kind='finish' order by id limit 1")]
    if only is None: random.shuffle(locs)
    n = len(locs)
    mults = [m for m in range(1, n + 1) if math.gcd(m, n) == 1] or [1]
    conn.execute("delete from steps" if only is None else f"delete from steps where team_id={int(only)}")
    conn.execute("delete from games" if only is None else f"delete from games where team_id={int(only)}")
    for i, t in enumerate(teams):
        if only is not None and t != only: continue
        m = mults[(i // n) % len(mults)] if n else 1
        order = [locs[(i + m * p) % n] for p in range(per_team(conn, n))] + fin
        for p, l in enumerate(order, 1):
            conn.execute("insert into steps(team_id,pos,location_id,variant) values(?,?,?,?)", (t, p, l, i // n if n else 0))
    conn.commit()

with sqlite3.connect(DB) as c:
    c.executescript(SCHEMA)
    c.execute("insert or ignore into settings values('code','0')")
    c.execute("insert or ignore into settings values('per_team','6')")
    if not c.execute("select count(*) from teams").fetchone()[0]:
        for n in ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet", "Kilo",
                  "Lima", "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango"]:
            c.execute("insert into teams(name,code) values(?,?)", ("Team " + n, secrets.token_hex(2).upper()))
        for n, k1, k2 in [("Library", "Jahan kitabein hain par shor mana hai.", "Jahan book issue karwane ke liye card lagta hai."),
                          ("Lab", "Jahan experiments hote hain aur coat pehna jaata hai.", "Jahan beaker aur oscilloscope rakhe milte hain."),
                          ("Auditorium", "Jahan bade function aur seminar hote hain.", "Jahan stage aur sainkdon kursiyan lagi hain."),
                          ("Ground", "Khula maidan jahan subah assembly hoti hai.", "Jahan jhanda fehraya jaata hai."),
                          ("Sports Ground", "Jahan cricket, football aur kabaddi ke match hote hain.", "Jahan stumps aur goal post lage milte hain."),
                          ("Civil Building", "Jahan naksho aur surveying ke instrument milte hain.", "Jahan bridges aur imaarton ke model dikhte hain."),
                          ("Canteen", "Jahan chai aur samose milte hain.", "Jahan bhookh ka ilaaj milta hai."),
                          ("Workshop", "Jahan lathe aur drilling machine chalti hain.", "Jahan welding ki chingariyan udti hain."),
                          ("Garden", "Jahan phool khilte hain aur bench par log baithte hain.", "Jahan ghaas aur ped-paudhe lage hain."),
                          ("Admin Block", "Jahan form jama hote hain aur office ka kaam hota hai.", "Jahan principal ka office hai.")]:
            lid = c.execute("insert into locations(name,slug,kind) values(?,?,'normal')", (n, n.lower().replace(" ", "-"))).lastrowid
            used = set()
            for v, k in enumerate([k1, k2]):
                while True:
                    a, b = random.randint(3, 9), random.randint(3, 9)
                    if a * b not in used: break
                used.add(a * b)
                c.execute("insert into riddles values(?,?,?,?,?)", (lid, v, k,
                          f"(Sample puzzle, apni se badlein) {a} x {b} kitna hota hai? Wahi aapka number code hai.", str(a * b)))
        c.execute("insert into locations(name,slug,kind) values('Classroom','classroom','start')")
        gen_routes(c)

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def q(sql, a=(), one=False):
    cur = db().execute(sql, a)
    return cur.fetchone() if one else cur.fetchall()

def w(sql, a=()):
    db().execute(sql, a); db().commit()

def req_code():
    r = q("select v from settings where k='code'", one=True)
    return bool(r and r["v"] == "1")

def content(loc_id, v):
    """Is location ke liye team ke variant ka (clue, puzzle, code)."""
    rows = q("select * from riddles where location_id=? order by variant", (loc_id,))
    return rows[(v or 0) % len(rows)] if rows else None

def game_for(step):
    return game_cfg(step)[0]

def game_cfg(step):
    """Is step par (game, level) ya (None, None). Finish par kabhi nahi."""
    if step["kind"] == "finish": return None, None
    k = q("select count(*) from steps s join locations l on l.id=s.location_id where s.team_id=? and l.kind!='finish'", (step["team_id"],), one=True)[0]
    return game_steps(k).get(step["pos"], (None, None))

def show(step):
    c = content(step["location_id"], step["variant"])
    g = game_for(step)
    return {"clue": c["clue"] if c else "", "puzzle": "" if g else (c["puzzle"] if c else ""), "game": g, "pos": step["pos"]}

STEPS = """select s.*, l.name lname, l.kind from steps s
           join locations l on l.id=s.location_id where s.team_id=? order by s.pos"""

BASE = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{% if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{% endif %}
<title>Treasure Hunt</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4f1ea;color:#222}
header{background:#2b2d42;color:#fff;padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header a{color:#ffd166;text-decoration:none}main{max-width:1000px;margin:auto;padding:16px}
.card{background:#fff;border-radius:10px;padding:14px;margin:12px 0;box-shadow:0 1px 4px #0002}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}
input,select,textarea,button{font:inherit;padding:8px;margin:3px 0;border-radius:6px;border:1px solid #bbb;max-width:100%}
button{background:#2b2d42;color:#fff;border:0;cursor:pointer}button.del{background:#c0392b}
.big{font-size:20px;background:#fff8dc;border:2px dashed #e0a800;border-radius:10px;padding:16px;margin:10px 0;white-space:pre-wrap}
.ok{color:#1e8449;font-weight:600}.err{color:#c0392b;font-weight:600}
@media print{header,.noprint{display:none}}
</style></head><body><header><b>🏴‍☠️ Treasure Hunt</b><a href="/leaderboard">Leaderboard</a>
<a href="/admin">Admin</a></header><main>{{ body|safe }}</main></body></html>"""

def page(tpl, refresh=None, **k):
    return R(BASE, body=R(tpl, **k), refresh=refresh)

def admin(f):
    @wraps(f)
    def inner(*a, **k):
        if not session.get("admin"): return redirect("/admin/login")
        return f(*a, **k)
    return inner

@app.route("/")
def home(): return redirect("/leaderboard")

@app.route("/leaderboard")
def leaderboard():
    rows = q("""select t.name,
      (select count(*) from steps s where s.team_id=t.id and s.done_ts is not null) n,
      (select count(*) from steps s where s.team_id=t.id) total,
      (select max(done_ts) from steps s where s.team_id=t.id) last,
      (select case when count(*)>0 and sum(done_ts is null)=0 then 1 else 0 end from steps s where s.team_id=t.id) fin
      from teams t order by fin desc, n desc, last asc""")
    return page("""<h2>🏆 Leaderboard</h2><div class="card"><table>
    <tr><th>#</th><th>Team</th><th>Progress</th><th>Last checkpoint time</th></tr>
    {% for r in rows %}<tr><td>{{ loop.index }}</td><td>{{ r.name }} {% if r.fin %}🏁{% endif %}</td>
    <td>{{ r.n }} / {{ r.total }}</td><td>{{ r.last or '-' }}</td></tr>{% endfor %}</table></div>""",
    refresh=15, rows=rows)


# ---------- games (server par jaanche jaate hain, jawab browser mein nahi chhupta) ----------
PHRASES = {"easy": ["TEAM WORK", "STAY CURIOUS", "KEEP GOING"],
           "medium": ["TEAM WORK WINS", "KNOWLEDGE IS POWER", "ENGINEERS BUILD DREAMS", "NEVER STOP LEARNING", "THINK BEFORE YOU ACT"],
           "hard": ["ENGINEERS BUILD THE FUTURE TOGETHER", "SMALL STEPS LEAD TO BIG CHANGE", "FAILURE IS THE FIRST STEP TO SUCCESS"]}
SHIFTS = {"easy": (1, 3), "medium": (3, 7), "hard": (8, 20)}
EMOJI = ["🍎", "🚀", "🌙", "🎲", "🔑", "🐢", "🎈", "🌵"]

def toggle(b, n, i):
    r, c = divmod(i, n)
    for nr, nc in ((r, c), (r+1, c), (r-1, c), (r, c+1), (r, c-1)):
        if 0 <= nr < n and 0 <= nc < n: b[nr * n + nc] ^= 1

def gen_game(kind, level="medium"):
    if level not in LEVELS: level = "medium"
    st = {"level": level}
    if kind == "sliding":                       # easy/medium 3x3 (kam/zyada ulti chaal), hard 4x4. Hamesha solvable.
        n = 4 if level == "hard" else 3
        t, z, last = list(range(1, n * n)) + [0], n * n - 1, -1
        for _ in range({"easy": 20, "medium": 60, "hard": 120}[level]):
            r, c = divmod(z, n)
            opts = [nr * n + nc for nr, nc in ((r+1, c), (r-1, c), (r, c+1), (r, c-1)) if 0 <= nr < n and 0 <= nc < n and nr * n + nc != last]
            m = random.choice(opts); t[z], t[m] = t[m], 0; last, z = z, m
        st["tiles"] = t
    elif kind == "cipher":
        st.update(plain=random.choice(PHRASES[level]), shift=random.randint(*SHIFTS[level]))
    elif kind == "sudoku":                      # 4x4, easy 6 / medium 9 / hard 12 khaali khane
        base = [[1, 2, 3, 4], [3, 4, 1, 2], [2, 1, 4, 3], [4, 3, 2, 1]]
        d = random.sample([1, 2, 3, 4], 4)
        rows = random.sample([0, 1], 2) + random.sample([2, 3], 2)
        cols = random.sample([0, 1], 2) + random.sample([2, 3], 2)
        flat = [d[base[r][c] - 1] for r in rows for c in cols]
        hide = set(random.sample(range(16), {"easy": 6, "medium": 9, "hard": 12}[level]))
        st["puzzle"] = [0 if i in hide else v for i, v in enumerate(flat)]
    elif kind == "memory":                      # easy 4 / medium 6 / hard 8 jodiyan
        cards = EMOJI[:{"easy": 4, "medium": 6, "hard": 8}[level]] * 2
        random.shuffle(cards); st["cards"] = cards
    elif kind == "lights":                      # easy 3x3, medium 4x4, hard 5x5; solved board par random dabaav (hamesha solvable)
        n = {"easy": 3, "medium": 4, "hard": 5}[level]
        while True:
            b = [0] * (n * n)
            for i in random.sample(range(n * n), {"easy": 3, "medium": 5, "hard": 9}[level]): toggle(b, n, i)
            if sum(b) >= 3: break
        st["board"] = b
    elif kind == "hanoi":                       # easy 3 / medium 4 / hard 5 disks
        st["n"] = {"easy": 3, "medium": 4, "hard": 5}[level]
    return st

def game_view(kind, st):
    if kind == "cipher":
        k = st["shift"]
        return {"shift": k, "cipher": "".join(chr((ord(ch) - 65 + k) % 26 + 65) if ch.isalpha() else ch for ch in st["plain"])}
    if kind == "memory": return st["cards"]
    if kind == "lights": return st["board"]
    if kind == "hanoi": return {"n": st["n"]}
    if kind == "sudoku": return st["puzzle"]
    return st["tiles"]

def check_game(kind, st, p):
    try:
        if kind == "memory":
            cards, m, done_ = st["cards"], p["moves"][:400], set()
            if len(m) % 2: return False
            for a, b in zip(m[0::2], m[1::2]):
                if not all(isinstance(x, int) and 0 <= x < len(cards) for x in (a, b)) or a == b or a in done_ or b in done_: return False
                if cards[a] == cards[b]: done_ |= {a, b}
            return len(done_) == len(cards)
        if kind == "lights":
            b = list(st["board"]); n = math.isqrt(len(b))
            for i in p["presses"][:500]:
                if not isinstance(i, int) or not 0 <= i < n * n: return False
                toggle(b, n, i)
            return not any(b)
        if kind == "sliding":
            t = list(st["tiles"]); N = len(t); n = math.isqrt(N)
            for i in p["moves"][:3000]:
                z = t.index(0)
                if not isinstance(i, int) or not 0 <= i < N or abs(i // n - z // n) + abs(i % n - z % n) != 1: return False
                t[z], t[i] = t[i], 0
            return t == list(range(1, N)) + [0]
        if kind == "cipher":
            return " ".join(str(p["answer"]).upper().split()) == st["plain"]
        if kind == "sudoku":
            g = p["grid"]
            if len(g) != 16 or any(a and a != b for a, b in zip(st["puzzle"], g)): return False
            rows = [g[i * 4:i * 4 + 4] for i in range(4)]
            cols = [g[i::4] for i in range(4)]
            boxes = [[g[r * 4 + c] for r in (br, br + 1) for c in (bc, bc + 1)] for br in (0, 2) for bc in (0, 2)]
            return all(sorted(x) == [1, 2, 3, 4] for x in rows + cols + boxes)
        if kind == "hanoi":
            d = st["n"]; full = list(range(d, 0, -1)); pegs = [list(full), [], []]
            for mv in p["moves"][:1000]:
                a, b = map(int, mv.split(">"))
                if a not in (0, 1, 2) or b not in (0, 1, 2) or not pegs[a] or (pegs[b] and pegs[b][-1] < pegs[a][-1]): return False
                pegs[b].append(pegs[a].pop())
            return pegs[2] == full
    except Exception:
        return False
    return False

def game_auth():
    f = request.form
    team = q("select * from teams where id=?", (f.get("team_id"),), one=True)
    if not team or (req_code() and team["code"] != f.get("code", "").strip().upper()): return None
    pend = next((s for s in q(STEPS, (team["id"],)) if not s["done_ts"]), None)
    if not pend or str(pend["pos"]) != f.get("pos") or not game_for(pend): return None
    return team, pend

@app.post("/game/start")
def game_start():
    a = game_auth()
    if not a: return {"error": "Game abhi available nahi hai."}, 403
    team, pend = a
    kind, level = game_cfg(pend)
    row = q("select state, kind from games where team_id=? and pos=?", (team["id"], pend["pos"]), one=True)
    if row and row["kind"] == kind and json.loads(row["state"]).get("level") == level: st = json.loads(row["state"])
    else:
        st = gen_game(kind, level)
        w("insert or replace into games values(?,?,?,?)", (team["id"], pend["pos"], kind, json.dumps(st)))
    return {"kind": kind, "state": game_view(kind, st)}

@app.post("/game/submit")
def game_submit():
    a = game_auth()
    if not a: return {"ok": False}, 403
    team, pend = a
    kind, level = game_cfg(pend)
    row = q("select state, kind from games where team_id=? and pos=?", (team["id"], pend["pos"]), one=True)
    try: payload = json.loads(request.form["payload"])
    except Exception: return {"ok": False}
    if not row or row["kind"] != kind or json.loads(row["state"]).get("level") != level or not check_game(kind, json.loads(row["state"]), payload): return {"ok": False}
    cur = content(pend["location_id"], pend["variant"])
    return {"ok": True, "code": (cur["code"] if cur else "")}

GAME_JS = r"""
(function(){
const G = window.GAME, box = document.getElementById('game');
const post = (u, extra) => { if (G.pv) u = '/admin/pv' + u; const f = new FormData(); if (G.pv) { f.append('kind', G.pv); f.append('level', G.level); } f.append('team_id', G.team); f.append('code', G.tcode); f.append('pos', G.pos);
  for (const k in (extra || {})) f.append(k, extra[k]); return fetch(u, {method: 'POST', body: f}).then(r => r.json()); };
const msg = t => { const m = box.querySelector('.gmsg'); if (m) m.textContent = t; };
function done(payload){ post('/game/submit', {payload: JSON.stringify(payload)}).then(r => {
  if (!r.ok) return msg('❌ Abhi sahi nahi hai, dobara koshish karein.');
  box.innerHTML = '<div class="big">🎉 Jeet gaye!' + (r.code ? '\nAapka number code: <b style="font-size:34px">' + r.code + '</b>\nIse agli jagah ka QR scan karke wahan daalein.' : '\nAb agli jagah jaayein.') + '</div>'; }); }
const GAMES = {
 sliding(el, t){ const moves = [], n = Math.round(Math.sqrt(t.length)), goal = t.map((_, i) => (i + 1) % t.length).join(), sz = n > 3 ? 68 : 80;
   const draw = () => { el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(' + n + ',' + sz + 'px);gap:6px;justify-content:center">' +
     t.map((v, i) => '<button data-i="' + i + '" style="height:' + sz + 'px;font-size:26px;' + (v ? '' : 'visibility:hidden') + '">' + v + '</button>').join('') +
     '</div><p>Tiles ko 1 se ' + (t.length - 1) + ' ke order mein lagayein (khaali jagah neeche-right). Chaal: ' + moves.length + '</p>';
    el.querySelectorAll('button').forEach(b => b.onclick = () => { const i = +b.dataset.i, z = t.indexOf(0);
     if (Math.abs((i / n | 0) - (z / n | 0)) + Math.abs(i % n - z % n) !== 1) return;
     t[z] = t[i]; t[i] = 0; moves.push(i); draw(); if (t.join() === goal) done({moves: moves}); }); };
   draw(); },
 memory(el, cards){ const moves = [], matched = new Set(); let open = [], lock = false;
  const draw = () => { el.innerHTML = '<p>Jodiyan milayein: ek ke baad ek do card kholein. Ek jaise hon to khule rahenge. Chaal: ' + (moves.length / 2) + '</p><div style="display:grid;grid-template-columns:repeat(4,64px);gap:8px;justify-content:center">' +
    cards.map((v, i) => '<button data-i="' + i + '" style="height:64px;font-size:30px;' + (matched.has(i) ? 'background:#a9dfbf;' : '') + '">' + ((matched.has(i) || open.includes(i)) ? v : '❓') + '</button>').join('') + '</div>';
   el.querySelectorAll('button').forEach(b => b.onclick = () => { if (lock) return; const i = +b.dataset.i; if (matched.has(i) || open.includes(i)) return;
    open.push(i);
    if (open.length < 2) return draw();
    const a = open[0], c = open[1]; moves.push(a, c);
    if (cards[a] === cards[c]) { matched.add(a); matched.add(c); open = []; draw(); if (matched.size === cards.length) done({moves: moves}); }
    else { draw(); lock = true; setTimeout(() => { open = []; lock = false; draw(); }, 800); } }); };
  draw(); },
 lights(el, L){ const presses = [], n = Math.round(Math.sqrt(L.length)), sz = Math.min(72, (280 / n) | 0);
   const draw = () => { el.innerHTML = '<p>Saari batti band (andhera) karni hai. Ek batti dabane se wo aur uske upar-neeche-aaju-baaju wali badal jaati hain. Chaal: ' + presses.length + '</p><div style="display:grid;grid-template-columns:repeat(' + n + ',' + sz + 'px);gap:6px;justify-content:center">' +
     L.map((v, i) => '<button data-i="' + i + '" style="height:' + sz + 'px;background:' + (v ? '#ffd166' : '#2b2d42') + '"></button>').join('') + '</div>';
    el.querySelectorAll('button').forEach(b => b.onclick = () => { const i = +b.dataset.i, r = i / n | 0, c = i % n;
     [[r, c], [r + 1, c], [r - 1, c], [r, c + 1], [r, c - 1]].forEach(p => { if (p[0] >= 0 && p[0] < n && p[1] >= 0 && p[1] < n) L[p[0] * n + p[1]] ^= 1; });
     presses.push(i); draw(); if (L.every(x => !x)) done({presses: presses}); }); };
   draw(); },
 cipher(el, s){ el.innerHTML = '<div class="big">' + s.cipher + '</div><p>Har akshar alphabet mein ' + s.shift + ' kadam aage khisaka hua hai (jaise A ki jagah ' +
   String.fromCharCode(65 + s.shift) + '). Original vaakya likhein:</p><input id="ca" style="width:100%;text-transform:uppercase"><button id="cb">Check</button>';
  document.getElementById('cb').onclick = () => done({answer: document.getElementById('ca').value}); },
 sudoku(el, p){ el.innerHTML = '<p>Har row, column aur 2x2 box mein 1 se 4 ek-ek baar aane chahiye.</p><div style="display:grid;grid-template-columns:repeat(4,56px);gap:4px;justify-content:center">' +
   p.map((v, i) => '<input ' + (v ? 'value="' + v + '" disabled' : '') + ' maxlength="1" inputmode="numeric" style="width:56px;height:56px;text-align:center;font-size:24px;' +
   (i % 4 === 1 ? 'margin-right:8px;' : '') + ((i / 4 | 0) === 1 ? 'margin-bottom:8px;' : '') + '">').join('') + '</div><button id="sb">Check</button>';
  document.getElementById('sb').onclick = () => done({grid: [...el.querySelectorAll('input')].map(x => +x.value || 0)}); },
 hanoi(el, s){ const N = s.n, pegs = [Array.from({length: N}, (_, i) => N - i), [], []], moves = []; let sel = -1;
  const draw = () => { el.innerHTML = '<p>Saari disks ko teesre peg par le jaayein. Bari disk chhoti ke upar nahi rakh sakte. Pehle peg chunein, phir jahan rakhni hai wo. Chaal: ' + moves.length + '</p>' +
   '<div style="display:flex;gap:10px;justify-content:center">' + pegs.map((p, i) => '<button data-i="' + i + '" style="width:30%;min-height:150px;display:flex;flex-direction:column-reverse;align-items:center;gap:3px;padding:6px;' +
   (sel === i ? 'outline:3px solid #e0a800;' : '') + '">' + p.map(d => '<span style="display:block;height:20px;width:' + (20 + d * (70 / N | 0)) + 'px;background:#ffd166;border-radius:4px;color:#222">' + d + '</span>').join('') + '</button>').join('') + '</div>';
   el.querySelectorAll('button').forEach(b => b.onclick = () => { const i = +b.dataset.i;
    if (sel < 0) { if (pegs[i].length) sel = i; }
    else { const a = pegs[sel], c = pegs[i];
     if (i !== sel && a.length && (!c.length || c[c.length - 1] > a[a.length - 1])) { c.push(a.pop()); moves.push(sel + '>' + i); }
     sel = -1; }
    draw(); if (pegs[2].length === N) done({moves: moves}); }); };
  draw(); }
};
post('/game/start').then(d => { if (d.error) { box.textContent = d.error; return; }
  box.innerHTML = '<div class="gmsg err"></div><div id="gb"></div>'; GAMES[d.kind](document.getElementById('gb'), d.state); });
})();
"""

# ---------- admin: games setup + preview ----------
PV = {}

@app.post("/admin/pv/game/start")
@admin
def pv_start():
    k, lv = request.form.get("kind"), request.form.get("level", "medium")
    if k not in GAME_KINDS: abort(404)
    PV[k] = gen_game(k, lv)
    return {"kind": k, "state": game_view(k, PV[k])}

@app.post("/admin/pv/game/submit")
@admin
def pv_submit():
    k = request.form.get("kind")
    try: ok = k in PV and check_game(k, PV[k], json.loads(request.form["payload"]))
    except Exception: ok = False
    return {"ok": ok, "code": "1234 (preview)"} if ok else {"ok": False}

@app.route("/admin/games", methods=["GET", "POST"])
@admin
def games_page():
    nloc = q("select count(*) from locations where kind='normal'", one=True)[0]
    kmax = per_team(db(), nloc)
    if request.method == "POST":
        if "auto" in request.form:
            w("delete from settings where k='games_cfg'")
        else:
            cfg = {}
            for p in range(1, kmax + 1):
                k, lv = request.form.get(f"k{p}", ""), request.form.get(f"l{p}", "medium")
                if k in GAME_KINDS and lv in LEVELS: cfg[str(p)] = [k, lv]
            w("insert or replace into settings values('games_cfg',?)", (json.dumps(cfg),))
        w("delete from games")                    # purane game-state hata do, naye settings ke hisaab se bante hain
        return redirect("/admin/games")
    manual = q("select v from settings where k='games_cfg'", one=True) is not None
    cur = game_steps(kmax)
    return page("""<h2>🎮 Games</h2>
    <div class="card"><h3>Kis step par kaun sa game</h3>
    <p>Har team ke route ke step (1 se {{ kmax }}) par aap game chun sakte hain aur uski kathinai (easy / medium / hard). Jis step par "— koi nahi —" hai wahan sadi puzzle aur code chalega.
    Abhi: <b>{{ 'aapki setting' if manual else 'auto (apne aap)' }}</b>.</p>
    <form method="post"><table><tr><th>Step</th><th>Game</th><th>Kathinai</th></tr>
    {% for p in range(1, kmax + 1) %}{% set c = cur.get(p) %}<tr><td>{{ p }}</td>
    <td><select name="k{{ p }}"><option value="">— koi nahi —</option>{% for k in kinds %}<option value="{{ k }}" {{ 'selected' if c and c[0] == k }}>{{ k }}</option>{% endfor %}</select></td>
    <td><select name="l{{ p }}">{% for l in levels %}<option value="{{ l }}" {{ 'selected' if (c[1] if c else 'medium') == l }}>{{ l }}</option>{% endfor %}</select></td></tr>{% endfor %}</table>
    <button>Save</button> <button name="auto" value="1" formnovalidate onclick="return confirm('Auto par wapas?')">Auto par wapas</button></form></div>
    <div class="card"><h3>Game test karein</h3><p>Koi bhi game turant khel kar dekhein. Jeetne par code "1234 (preview)" dikhega.</p>
    <form method="get" onsubmit="location.href='/admin/games/'+this.k.value+'?level='+this.l.value; return false">
    <select name="k">{% for k in kinds %}<option>{{ k }}</option>{% endfor %}</select>
    <select name="l">{% for l in levels %}<option {{ 'selected' if l == 'medium' }}>{{ l }}</option>{% endfor %}</select> <button>Khelein</button></form></div>""",
    kinds=GAME_KINDS, levels=LEVELS, kmax=kmax, cur=cur, manual=manual)

@app.route("/admin/games/<kind>")
@admin
def game_preview(kind):
    if kind not in GAME_KINDS: abort(404)
    level = request.args.get("level", "medium")
    return page("""<h2>🎮 {{ kind }} ({{ level }}) — preview</h2><div id="game" class="card">Game load ho raha hai...</div>
    <script>window.GAME={team:0,tcode:"",pos:0,pv:{{ kind|tojson }},level:{{ level|tojson }}};</script><script>{{ gjs|safe }}</script>
    <p><a href="/admin/games">← Games</a></p>""", kind=kind, level=level, gjs=GAME_JS)

# ---------- participant side ----------
@app.route("/s/<slug>", methods=["GET", "POST"])
def scan(slug):
    loc = q("select * from locations where slug=?", (slug,), one=True) or abort(404)
    teams = q("select id,name from teams order by name")
    msg, res, team = None, {}, None
    if request.method == "POST":
        team = q("select * from teams where id=?", (request.form.get("team_id"),), one=True)
        tcode = request.form.get("code", "").strip().upper()
        if not team or (req_code() and team["code"] != tcode):
            msg, team = "❌ Team code galat hai.", None
        else:
            steps = q(STEPS, (team["id"],))
            pend = next((s for s in steps if not s["done_ts"]), None)
            ans = request.form.get("answer")
            if not steps: msg = "Aapka route set nahi hai, organizer se milein."
            elif pend is None: res["finished"] = True
            elif loc["kind"] == "start": res.update(show(pend)); res["first"] = True
            elif pend["location_id"] == loc["id"]:
                tp = (team["id"], pend["pos"])
                w(f"update steps set scan_ts=coalesce(scan_ts,{NOW}) where team_id=? and pos=?", tp)
                cur = content(pend["location_id"], pend["variant"])
                need = (cur["code"] if cur else "").strip().replace(" ", "").lower()
                if need and ans is None: res["gate"] = True
                elif need and ans.strip().replace(" ", "").lower() != need:
                    msg, res["gate"] = "❌ Code galat hai, paheli dobara dekhein aur phir try karein.", True
                else:
                    w(f"update steps set done_ts={NOW} where team_id=? and pos=?", tp)
                    nxt = steps[pend["pos"]] if pend["pos"] < len(steps) else None
                    if nxt: res.update(show(nxt))
                    else: res["finished"] = True
            elif any(s["location_id"] == loc["id"] and s["done_ts"] for s in steps):
                res.update(show(pend)); res["again"] = True
            else: msg = "⚠️ Ye aapka agla checkpoint nahi hai. Apni pichli clue dobara padhein."
    return page("""<h2>📍 {{ loc.name }}</h2>
    {% if res.get('finished') %}<div class="big">🎉 Badhai ho {{ team.name }}! Aapne saari paheliyan solve kar li, aap finish line par hain!</div>
    {% elif res.get('gate') %}{% if msg %}<p class="err">{{ msg }}</p>{% else %}<p class="ok">✅ Sahi jagah par pahunche, {{ team.name }}!</p>{% endif %}
      <form method="post" class="card"><input type="hidden" name="team_id" value="{{ team.id }}">
      <input type="hidden" name="code" value="{{ tcode }}">
      <label><b>Pichli paheli solve karke jo number code mila wo yahan daalein:</b></label><br>
      <input name="answer" required autocomplete="off" inputmode="numeric" placeholder="Number code" style="width:100%;font-size:20px">
      <button style="width:100%">Unlock</button></form>
    {% elif 'clue' in res %}<p class="ok">{{ '🔁 Aapki current clue aur paheli:' if (res.get('again') or res.get('first')) else '✅ Code sahi! Ye rahi aapki agli clue aur paheli:' }}</p>
      {% if res.clue %}<div class="big">📍 {{ res.clue }}</div>{% endif %}
      {% if res.get('game') %}<div class="big">🎮 Is baar ek game khelna hai! Jeetne par aapko number code milega.</div>
      <div id="game" class="card">Game load ho raha hai...</div>
      <script>window.GAME={team:{{ team.id }},tcode:{{ tcode|tojson }},pos:{{ res.pos }}};</script><script>{{ gjs|safe }}</script>
      {% elif res.puzzle %}<div class="big">🧩 {{ res.puzzle }}</div>
      <p>Paheli solve karke jo number code mile, use agli jagah ka QR scan karke wahan daalein.</p>{% endif %}
    {% else %}{% if msg %}<p class="err">{{ msg }}</p>{% endif %}
    <form method="post"><p><b>Apni team ke naam par click karein</b></p>
    {% if rc %}<input name="code" placeholder="Team code" required autocomplete="off" style="text-transform:uppercase;width:100%"><br>{% endif %}
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-top:8px">
    {% for t in teams %}<button name="team_id" value="{{ t.id }}" style="padding:14px 6px;font-size:16px">{{ t.name }}</button>{% endfor %}
    </div></form>{% endif %}""", loc=loc, teams=teams, msg=msg, res=res, team=team,
    tcode=request.form.get("code", "").strip().upper(), rc=req_code(), gjs=GAME_JS)

# ---------- admin ----------
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST" and request.form.get("p") == ADMIN_PASS:
        session["admin"] = True; return redirect("/admin")
    return page("""<form method="post" class="card"><h3>Admin login</h3>
    <input type="password" name="p" placeholder="Password"><button>Login</button></form>""")

@app.route("/admin/logout")
def logout(): session.clear(); return redirect("/")

@app.route("/admin")
@admin
def dash():
    teams, locs = q("select * from teams order by name"), q("select * from locations order by id")
    routes = {}
    for s in q("select s.*, l.name lname from steps s join locations l on l.id=s.location_id order by team_id,pos"):
        routes.setdefault(s["team_id"], []).append(s)
    return page("""<h2>Dashboard</h2>
    <div class="card"><h3>Progress (kaun kahan pahuncha)</h3>
    <p>✅ code sahi, aage badh gayi &nbsp; ⏳ pahunch gayi, code baaki &nbsp; · abhi nahi pahunchi</p><div style="overflow-x:auto"><table>
    <tr><th>Team</th><th>Code</th><th>Done</th><th>Route</th><th></th></tr>
    {% for t in teams %}{% set rt = routes.get(t.id, []) %}<tr>
    <td><form method="post" action="/admin/team/{{ t.id }}/rename"><input name="name" value="{{ t.name }}" size="12"><button>Save</button></form></td>
    <td><code>{{ t.code }}</code></td><td>{{ rt|selectattr('done_ts')|list|length }}/{{ rt|length }}</td>
    <td>{% for s in rt %}{{ '✅' if s.done_ts else ('⏳' if s.scan_ts else '·') }}{{ s.lname }}{% if not loop.last %} → {% endif %}{% endfor %}</td>
    <td><a href="/admin/team/{{ t.id }}/route">Route</a>
    <form method="post" action="/admin/team/{{ t.id }}/delete" onsubmit="return confirm('Delete {{ t.name }}?')"><button class="del">Delete</button></form></td></tr>{% endfor %}</table></div></div>
    <div class="card"><h3>Team add karein</h3><form method="post" action="/admin/team">
    <input name="name" placeholder="Team name" required><button>Add</button></form>
    <h3>Team code: {{ 'ON' if rc else 'OFF' }}</h3><form method="post" action="/admin/toggle_code">
    <button>{{ 'OFF karein' if rc else 'ON karein (naam + code)' }}</button></form></div>
    <div class="card"><h3>Locations (clue / puzzle / code + QR)</h3><table>
    {% for l in locs %}<tr><td>{{ l.name }} <small>({{ l.kind }})</small></td><td><a href="/admin/loc/{{ l.id }}">Edit + QR</a></td>
    <td><form method="post" action="/admin/loc/{{ l.id }}/delete" onsubmit="return confirm('Delete?')"><button class="del">Delete</button></form></td></tr>{% endfor %}</table>
    <form method="post" action="/admin/loc"><input name="name" placeholder="Location name" required>
    <select name="kind"><option value="normal">Normal</option><option value="finish">Finish</option><option value="start">Start</option></select>
    <button>Add location</button></form>
    <p><small>Naya location jodne ke baad Routes page se "Routes dobara banayein" dabayein.</small></p></div>
    <div class="card"><a href="/admin/routes">🗺️ Routes</a> &nbsp;|&nbsp; <a href="/admin/games">🎮 Games</a> &nbsp;|&nbsp; <a href="/admin/logout">Logout</a></div>""",
    teams=teams, locs=locs, routes=routes, rc=req_code())

@app.post("/admin/toggle_code")
@admin
def toggle_code():
    w("update settings set v=? where k='code'", ("0" if req_code() else "1",)); return redirect("/admin")

@app.post("/admin/team")
@admin
def add_team():
    try:
        cur = db().execute("insert into teams(name,code) values(?,?)", (request.form["name"].strip(), secrets.token_hex(2).upper()))
        db().commit(); gen_routes(db(), only=cur.lastrowid)
    except sqlite3.IntegrityError: pass
    return redirect("/admin")

@app.post("/admin/team/<int:i>/rename")
@admin
def rename_team(i):
    try: w("update teams set name=? where id=?", (request.form["name"].strip(), i))
    except sqlite3.IntegrityError: pass
    return redirect("/admin")

@app.post("/admin/team/<int:i>/delete")
@admin
def del_team(i):
    w("delete from teams where id=?", (i,)); w("delete from steps where team_id=?", (i,)); w("delete from games where team_id=?", (i,)); return redirect("/admin")

@app.post("/admin/loc")
@admin
def add_loc():
    lid = db().execute("insert into locations(name,slug,kind) values(?,?,?)",
                       (request.form["name"].strip(), secrets.token_urlsafe(6), request.form["kind"])).lastrowid
    for v in (0, 1): db().execute("insert into riddles(location_id,variant) values(?,?)", (lid, v))
    db().commit(); return redirect("/admin")

@app.post("/admin/loc/<int:i>/delete")
@admin
def del_loc(i):
    for t in ("locations where id", "steps where location_id", "riddles where location_id"): db().execute(f"delete from {t}=?", (i,))
    db().commit(); return redirect("/admin")

@app.route("/admin/loc/<int:i>", methods=["GET", "POST"])
@admin
def loc_page(i):
    loc = q("select * from locations where id=?", (i,), one=True) or abort(404)
    if request.method == "POST":
        f, act = request.form, request.form.get("act", "save")
        db().execute("update locations set name=? where id=?", (f["name"].strip(), i))
        for r in q("select variant from riddles where location_id=?", (i,)):
            v = r["variant"]
            if f"clue_{v}" in f:
                db().execute("update riddles set clue=?,puzzle=?,code=? where location_id=? and variant=?",
                             (f[f"clue_{v}"].strip(), f[f"puzzle_{v}"].strip(), f[f"code_{v}"].strip(), i, v))
        if act == "add":
            mx = q("select coalesce(max(variant),-1) m from riddles where location_id=?", (i,), one=True)["m"]
            db().execute("insert into riddles(location_id,variant) values(?,?)", (i, mx + 1))
        elif act.startswith("del_"): db().execute("delete from riddles where location_id=? and variant=?", (i, int(act[4:])))
        db().commit(); return redirect(f"/admin/loc/{i}")
    vs = q("select * from riddles where location_id=? order by variant", (i,))
    url = request.host_url + "s/" + loc["slug"]
    return page("""<h2>{{ loc.name }} <small>({{ loc.kind }})</small></h2><div class="card" style="text-align:center">
    <div id="qr" style="display:inline-block"></div><p><code>{{ url }}</code></p>
    <button class="noprint" onclick="print()">Print QR</button></div>
    <form method="post" class="noprint"><div class="card"><label>Naam</label><br><input name="name" value="{{ loc.name }}"></div>
    {% if loc.kind != 'start' %}<div class="card"><p><b>Is jagah ke liye clue + puzzle + code.</b> Team ko clue aur puzzle pichli jagah (ya start) par dikhte hain.
    Puzzle solve karke jo <b>number code</b> mile, use team is jagah ka QR scan karke daalegi. Kuch steps par puzzle ki jagah game aata hai (Admin → Games se tay karein); clue aur code tab bhi yahin ke chalte hain. Alag-alag team group ko alag set milta hai,
    isliye ek hi jagah jaane wali do teams ko alag puzzle/code milega (kam se kam 2 set rakhein).</p></div>
    {% for v in vs %}<div class="card"><h3>Set {{ v.variant + 1 }}</h3>
    <label>Clue (is jagah ka rasta)</label><br><textarea name="clue_{{ v.variant }}" rows="2" style="width:100%">{{ v.clue }}</textarea><br>
    <label>Puzzle (jigsaw / word riddle ka description)</label><br><textarea name="puzzle_{{ v.variant }}" rows="3" style="width:100%">{{ v.puzzle }}</textarea><br>
    <label>Number code (puzzle ka jawab)</label> <input name="code_{{ v.variant }}" value="{{ v.code }}" size="10">
    <button class="del" name="act" value="del_{{ v.variant }}" onclick="return confirm('Is set ko delete karein?')">Delete set</button></div>{% endfor %}
    <button name="act" value="add">+ Naya set</button> {% endif %}<button name="act" value="save">Save</button></form>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <script>new QRCode(document.getElementById('qr'),{text:{{ url|tojson }},width:260,height:260});</script>""", loc=loc, vs=vs, url=url)

@app.route("/admin/routes", methods=["GET", "POST"])
@admin
def routes_page():
    nloc = q("select count(*) from locations where kind='normal'", one=True)[0]
    if request.method == "POST":
        try: k = max(1, min(int(request.form.get("per_team", "0")), nloc))
        except ValueError: k = nloc
        w("insert or replace into settings values('per_team',?)", (str(k),))
        gen_routes(db()); return redirect("/admin/routes")
    cur_k = per_team(db(), nloc)
    rows = {}
    for s in q("select s.team_id, s.pos, l.name lname from steps s join locations l on l.id=s.location_id order by team_id,pos"):
        rows.setdefault(s["team_id"], []).append(s["lname"])
    return page("""<h2>🗺️ Routes</h2><div class="card"><p>Har team ko alag order milta hai taaki sab ek hi jagah par na jamen.</p>
    <form method="post" onsubmit="return confirm('Sabhi routes dobara bante hain aur progress reset ho jayegi. Pakka?')">
    Har team kitni locations par jayegi (kul {{ nloc }} mein se): <input name="per_team" type="number" min="1" max="{{ nloc }}" value="{{ cur_k }}" style="width:70px">
    <button class="del">Save aur routes dobara banayein (progress reset)</button></form></div>
    <div class="card"><table>{% for t in teams %}<tr><td><a href="/admin/team/{{ t.id }}/route">{{ t.name }}</a></td>
    <td>{{ rows.get(t.id, [])|join(' → ') }}</td></tr>{% endfor %}</table></div>""", teams=q("select * from teams order by name"), rows=rows, nloc=nloc, cur_k=cur_k)

@app.route("/admin/team/<int:i>/route", methods=["GET", "POST"])
@admin
def team_route(i):
    t = q("select * from teams where id=?", (i,), one=True) or abort(404)
    locs = q("select * from locations where kind!='start' order by id")
    n = len(locs)
    if request.method == "POST":
        v = q("select variant from steps where team_id=? limit 1", (i,), one=True)
        v = v[0] if v else 0
        w("delete from steps where team_id=?", (i,))
        for p in range(1, n + 1):
            if request.form.get(f"p{p}"): db().execute("insert into steps(team_id,pos,location_id,variant) values(?,?,?,?)", (i, p, request.form[f"p{p}"], v))
        db().commit(); return redirect("/admin/routes")
    cur = {s["pos"]: s["location_id"] for s in q("select pos,location_id from steps where team_id=?", (i,))}
    return page("""<h2>Route: {{ t.name }}</h2><form method="post" class="card"><p>Save karne par is team ki progress reset hogi.</p>
    {% for p in range(1, n + 1) %}Step {{ p }}: <select name="p{{ p }}"><option value="">--</option>
    {% for l in locs %}<option value="{{ l.id }}" {{ 'selected' if cur.get(p) == l.id }}>{{ l.name }}</option>{% endfor %}</select><br>{% endfor %}
    <button>Save route</button></form>""", t=t, locs=locs, n=n, cur=cur)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
