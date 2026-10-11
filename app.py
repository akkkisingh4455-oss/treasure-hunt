import os, json, hmac, time, hashlib, sqlite3, secrets, random, math
import urllib.request, urllib.parse
from functools import wraps
from flask import Flask, request, redirect, session, g, abort, Response, render_template_string as R
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hunt.db")
NOW = "datetime('now','localtime')"

# At which step (checkpoint number) a GAME appears in place of the puzzle. No game at the first step (start).
# Options: memory, cipher, sliding, lights, sudoku, hanoi, scramble, quiz, pattern, nonogram, codebreaker, wordsearch, rushhour, sudoku9. To change, edit here, e.g. {2: "cipher", 4: "sliding"}
# Games are now set by the admin: Admin -> "🎮 Games". Only the default (auto) applies here until the admin saves something.
GAME_KINDS = ["memory", "cipher", "sliding", "lights", "sudoku", "hanoi", "scramble", "quiz", "pattern", "nonogram", "codebreaker", "wordsearch", "rushhour", "sudoku9"]
LEVELS = ["easy", "medium", "hard"]
GAME_ORDER = ["memory", "cipher", "sliding", "lights"]
AUTO_LEVEL = {"memory": "easy", "cipher": "medium", "sliding": "medium", "lights": "hard"}

def game_steps(k):
    """k = number of normal locations for the team. Return {step: (game, level)}."""
    r = q("select v from settings where k='games_cfg'", one=True)
    if r:                                         # set manually by the admin
        try: cfg = json.loads(r["v"])
        except Exception: cfg = {}
        return {int(p): (v[0], v[1] if v[1] in LEVELS else "medium") for p, v in cfg.items() if int(p) <= k and v[0] in GAME_KINDS}
    pos, prev = [], 0                             # auto: 4 games from easy to hard, spread evenly along the route
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
create table if not exists settings(k text primary key, v text);
create table if not exists rounds(id integer primary key, ts text, label text, data text);"""

def per_team(conn, n):
    """How many normal locations each team visits (changed by the admin). 0/invalid = all."""
    r = conn.execute("select v from settings where k='per_team'").fetchone()
    try: k = int(r[0]) if r else 0
    except Exception: k = 0
    return n if k <= 0 else min(k, n)

def pick_variant(conn, loc, team):
    """Which clue/puzzle/code set this team gets for this location: the set used by the FEWEST other teams so far
    (lowest number first). So every team gets a different set for the same place; sets repeat only if the
    location has fewer sets than the number of teams visiting it."""
    n_sets = conn.execute("select count(*) from riddles where location_id=?", (loc,)).fetchone()[0] or 1
    used = {v: 0 for v in range(n_sets)}
    for (v,) in conn.execute("select variant from steps where location_id=? and team_id!=?", (loc, team)):
        if v is not None and v >= 0: used[v % n_sets] += 1
    return min(used, key=lambda v: (used[v], v))

def fix_variants(conn):
    """Re-assign clue sets so that no two teams get the same set for the same place. Progress is NOT reset:
    steps whose clue the team has already seen (up to the step after their last completed one) are left as they are."""
    teams = [r[0] for r in conn.execute("select id from teams order by id")]
    anydone = conn.execute("select count(*) from steps where done_ts is not null").fetchone()[0]
    seen = {}
    for t in teams:
        done = conn.execute("select count(*) from steps where team_id=? and done_ts is not null", (t,)).fetchone()[0]
        seen[t] = (done + 1) if anydone else 0
        conn.execute("update steps set variant=-1 where team_id=? and pos>?", (t, seen[t]))      # free these first, then hand out fresh
    for t in teams:
        for pos, loc in conn.execute("select pos, location_id from steps where team_id=? and variant<0 order by pos", (t,)).fetchall():
            conn.execute("update steps set variant=? where team_id=? and pos=?", (pick_variant(conn, loc, t), t, pos))
    conn.commit()

def gen_routes(conn, only=None):
    """Give each team a different order. With 10 locations and 20 teams: each group of 10 teams gets a different 'stride' (1,3,7,9),
    so that two teams reaching the same place get different next locations. variant = the team's group (clue/puzzle/code set)."""
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
            conn.execute("insert into steps(team_id,pos,location_id,variant) values(?,?,?,?)", (t, p, l, pick_variant(conn, l, t)))
    conn.commit()

with sqlite3.connect(DB) as c:
    c.executescript(SCHEMA)
    if os.environ.get("RESET_ADMIN_PASS"): c.execute("delete from settings where k in ('admin_pw','helper_pw','pw_pending')")   # forgot the password: start once with RESET_ADMIN_PASS=1
    c.execute("insert or ignore into settings values('code','0')")
    c.execute("insert or ignore into settings values('per_team','6')")
    if not c.execute("select count(*) from teams").fetchone()[0]:
        for n in ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet", "Kilo",
                  "Lima", "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango"]:
            c.execute("insert into teams(name,code) values(?,?)", ("Team " + n, secrets.token_hex(2).upper()))
        for n, k1, k2 in [("Library", "Where there are books but noise is not allowed.", "Where you need a card to get a book issued."),
                          ("Lab", "Where experiments are done and a coat is worn.", "Where beakers and oscilloscopes are kept."),
                          ("Auditorium", "Where big functions and seminars are held.", "Where there is a stage and hundreds of chairs."),
                          ("Ground", "An open field where the morning assembly takes place.", "Where the flag is hoisted."),
                          ("Sports Ground", "Where cricket, football and kabaddi matches are played.", "Where stumps and goal posts are set up."),
                          ("Civil Building", "Where maps and surveying instruments are found.", "Where models of bridges and buildings are displayed."),
                          ("Canteen", "Where tea and samosas are available.", "Where the cure for hunger is found."),
                          ("Workshop", "Where lathe and drilling machines run.", "Where welding sparks fly."),
                          ("Garden", "Where flowers bloom and people sit on benches.", "Where grass and plants are growing."),
                          ("Admin Block", "Where forms are submitted and office work is done.", "Where the principal's office is.")]:
            lid = c.execute("insert into locations(name,slug,kind) values(?,?,'normal')", (n, n.lower().replace(" ", "-"))).lastrowid
            used = set()
            for v, k in enumerate([k1, k2]):
                while True:
                    a, b = random.randint(3, 9), random.randint(3, 9)
                    if a * b not in used: break
                used.add(a * b)
                c.execute("insert into riddles values(?,?,?,?,?)", (lid, v, k,
                          f"(Sample puzzle, replace with your own) What is {a} x {b}? That is your number code.", str(a * b)))
        c.execute("insert into locations(name,slug,kind) values('Classroom','classroom','start')")
        gen_routes(c)

with sqlite3.connect(DB) as c: fix_variants(c)

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
    """The (clue, puzzle, code) for this location for the team's variant."""
    rows = q("select * from riddles where location_id=? order by variant", (loc_id,))
    return rows[(v or 0) % len(rows)] if rows else None

def game_for(step):
    return game_cfg(step)[0]

def game_cfg(step):
    """(game, level) at this step, or (None, None). Never at the finish."""
    if step["kind"] == "finish": return None, None
    k = q("select count(*) from steps s join locations l on l.id=s.location_id where s.team_id=? and l.kind!='finish'", (step["team_id"],), one=True)[0]
    return game_steps(k).get(step["pos"], (None, None))

def show(step):
    c = content(step["location_id"], step["variant"])
    g = game_for(step)
    return {"clue": c["clue"] if c else "", "puzzle": "" if g else (c["puzzle"] if c else ""), "game": g, "pos": step["pos"]}

STEPS = """select s.*, l.name lname, l.kind from steps s
           join locations l on l.id=s.location_id where s.team_id=? order by s.pos"""

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
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


# ---------- games (checked on the server; the answer is not hidden in the browser) ----------
PHRASES = {"easy": ["TEAM WORK", "STAY CURIOUS", "KEEP GOING"],
           "medium": ["TEAM WORK WINS", "KNOWLEDGE IS POWER", "ENGINEERS BUILD DREAMS", "NEVER STOP LEARNING", "THINK BEFORE YOU ACT"],
           "hard": ["ENGINEERS BUILD THE FUTURE TOGETHER", "SMALL STEPS LEAD TO BIG CHANGE", "FAILURE IS THE FIRST STEP TO SUCCESS"]}
SHIFTS = {"easy": (1, 3), "medium": (3, 7), "hard": (8, 20)}
EMOJI = ["🍎", "🚀", "🌙", "🎲", "🔑", "🐢", "🎈", "🌵"]

# --- Word scramble (engineering words), Quiz (college / mechanical) and Number pattern: easy / medium / hard ---
WORDS = {"easy": ["GEAR", "LATHE", "SHAFT", "BOLT", "VALVE", "PUMP", "CLUTCH", "WELD"],
         "medium": ["PISTON", "TORQUE", "BEARING", "TURBINE", "GASKET", "BOILER", "FLANGE", "WELDING"],
         "hard": ["CAMSHAFT", "FLYWHEEL", "CRANKSHAFT", "RADIATOR", "HYDRAULIC", "CARBURETOR", "GOVERNOR", "DYNAMOMETER", "TRANSMISSION"]}
COUNT = {"easy": 3, "medium": 4, "hard": 5}
# (question, correct answer, [3 wrong answers]) -- edit or add your own college / department questions here
QUIZ = {"easy": [("What is the SI unit of force?", "Newton", ["Joule", "Watt", "Pascal"]),
                 ("What is the SI unit of power?", "Watt", ["Newton", "Joule", "Kelvin"]),
                 ("What does CNC stand for?", "Computer Numerical Control", ["Central Network Control", "Computer Network Cutting", "Control Numerical Computer"]),
                 ("Which instrument measures small lengths accurately up to 0.01 mm?", "Micrometer", ["Meter scale", "Measuring tape", "Protractor"]),
                 ("Which of these is a renewable energy source?", "Solar", ["Coal", "Petrol", "Diesel"]),
                 ("Water boils at ___ °C at sea level.", "100", ["90", "80", "120"]),
                 ("Which metal is the best of these for electrical wires?", "Copper", ["Rubber", "Glass", "Wood"])],
        "medium": [("Which cycle is used in petrol engines?", "Otto cycle", ["Diesel cycle", "Rankine cycle", "Brayton cycle"]),
                   ("Which law says stress is proportional to strain within the elastic limit?", "Hooke's law", ["Ohm's law", "Boyle's law", "Newton's law"]),
                   ("Which machine tool makes cylindrical parts by rotating the workpiece?", "Lathe", ["Shaper", "Planer", "Milling machine"]),
                   ("Which process joins metals by melting them together?", "Welding", ["Rolling", "Forging", "Drawing"]),
                   ("Which cycle is used in steam power plants?", "Rankine cycle", ["Otto cycle", "Diesel cycle", "Stirling cycle"]),
                   ("What is the SI unit of pressure?", "Pascal", ["Newton", "Joule", "Watt"]),
                   ("What does a flywheel do in an engine?", "Smooths out speed fluctuations", ["Cools the engine", "Filters the air", "Injects the fuel"]),
                   ("Which gears connect parallel shafts with teeth parallel to the axis?", "Spur gears", ["Worm gears", "Bevel gears", "Rack gears"]),
                   ("The efficiency of an ideal Carnot engine depends on?", "Source and sink temperatures", ["Type of fuel", "Engine size", "Engine speed"])],
        "hard": [("Reynolds number tells whether a flow is...", "Laminar or turbulent", ["Hot or cold", "Fast or slow", "Steady or unsteady"]),
                 ("Which failure theory is based on maximum shear stress?", "Tresca theory", ["Rankine theory", "St. Venant theory", "Beltrami theory"]),
                 ("Which mode of heat transfer needs no medium?", "Radiation", ["Conduction", "Convection", "Diffusion"]),
                 ("In the ideal Otto cycle, heat is added at constant...", "Volume", ["Pressure", "Temperature", "Entropy"]),
                 ("In the ideal Diesel cycle, heat is added at constant...", "Pressure", ["Volume", "Temperature", "Enthalpy"]),
                 ("Which cycle has two isothermal and two constant-volume processes?", "Stirling cycle", ["Ericsson cycle", "Otto cycle", "Brayton cycle"]),
                 ("Poisson's ratio of most metals is roughly...", "0.25 to 0.35", ["1.5 to 2.0", "0.8 to 1.0", "5 to 10"]),
                 ("Mach number is the ratio of...", "Flow speed to speed of sound", ["Mass to volume", "Force to area", "Pressure to temperature"]),
                 ("The unit of Young's modulus is...", "N/m²", ["N·m", "N/m", "kg/m³"])]}

def gen_pattern(level):
    """One number pattern: show the terms, the team gives the next one."""
    kind = random.choice({"easy": ["arith", "geo2"], "medium": ["quad", "geo3", "sq"], "hard": ["fib", "alt", "cube"]}[level])
    if kind == "arith":
        a, d = random.randint(2, 9), random.randint(2, 9); t = [a + d * i for i in range(6)]
    elif kind == "geo2":
        a = random.randint(1, 5); t = [a * 2 ** i for i in range(6)]
    elif kind == "quad":                          # the differences grow by a constant
        t = [random.randint(1, 9)]; d, e = random.randint(1, 4), random.randint(1, 3)
        for i in range(5): t.append(t[-1] + d + i * e)
    elif kind == "geo3":
        a = random.randint(1, 3); t = [a * 3 ** i for i in range(6)]
    elif kind == "sq":
        c = random.randint(0, 9); t = [(i + 2) ** 2 + c for i in range(6)]
    elif kind == "fib":                           # each term is the sum of the previous two
        t = [random.randint(1, 5), random.randint(2, 8)]
        for _ in range(5): t.append(t[-1] + t[-2])
    elif kind == "alt":                           # +x, then x y, then +x ...
        t = [random.randint(1, 5)]; x, y = random.randint(2, 7), random.randint(2, 3)
        for i in range(6): t.append(t[-1] + x if i % 2 == 0 else t[-1] * y)
    else:
        c = random.randint(0, 9); t = [(i + 1) ** 3 + c for i in range(6)]
    return {"s": t[:-1], "a": t[-1]}

def toggle(b, n, i):
    r, c = divmod(i, n)
    for nr, nc in ((r, c), (r+1, c), (r-1, c), (r, c+1), (r, c-1)):
        if 0 <= nr < n and 0 <= nc < n: b[nr * n + nc] ^= 1


# --- Longer games: Nonogram, Code breaker (Mastermind), Word search ---
def nono_line(a):
    out, run = [], 0
    for v in a:
        if v: run += 1
        elif run: out.append(run); run = 0
    if run: out.append(run)
    return out or [0]

def gen_nono(level):
    """Random picture; the team only gets the number clues. Any grid that matches all clues is accepted."""
    n = {"easy": 5, "medium": 7, "hard": 8}[level]; dens = {"easy": .6, "medium": .55, "hard": .5}[level]
    while True:
        g = [1 if random.random() < dens else 0 for _ in range(n * n)]
        if all(any(g[i * n:(i + 1) * n]) for i in range(n)) and all(any(g[i::n]) for i in range(n)): break
    return g, [nono_line(g[i * n:(i + 1) * n]) for i in range(n)], [nono_line(g[i::n]) for i in range(n)]

CB = {"easy": (3, 5, False), "medium": (4, 6, False), "hard": (5, 6, True)}     # (code length, colours, colours may repeat)

def mm_fb(code, guess):
    """[right colour+place, right colour wrong place] or None if the guess is malformed."""
    if not isinstance(guess, list) or len(guess) != len(code) or any(not isinstance(x, int) or not 0 <= x < 10 for x in guess): return None
    black = sum(a == b for a, b in zip(code, guess))
    return [black, sum(min(code.count(c), guess.count(c)) for c in set(guess)) - black]

DIRS8 = [(0, 1), (1, 0), (1, 1), (-1, 1), (0, -1), (-1, 0), (-1, -1), (1, -1)]

def gen_wordsearch(level):
    n, k, nd = {"easy": (8, 5, 2), "medium": (10, 7, 3), "hard": (12, 9, 8)}[level]
    allw = sorted({x for v in WORDS.values() for x in v})
    lo, hi = {"easy": (4, 6), "medium": (5, 8), "hard": (6, 12)}[level]
    pool = [x for x in allw if lo <= len(x) <= min(hi, n)]
    while True:
        ws = random.sample(pool, k); g = [[""] * n for _ in range(n)]; ok = True
        for x in sorted(ws, key=len, reverse=True):
            for _ in range(300):
                dr, dc = random.choice(DIRS8[:nd]); r, c = random.randrange(n), random.randrange(n)
                cells = [(r + dr * i, c + dc * i) for i in range(len(x))]
                if not (0 <= cells[-1][0] < n and 0 <= cells[-1][1] < n): continue
                if all(g[a][b] in ("", ch) for (a, b), ch in zip(cells, x)):
                    for (a, b), ch in zip(cells, x): g[a][b] = ch
                    break
            else:
                ok = False; break
        if ok: break
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {"grid": ["".join(ch or random.choice(letters) for ch in row) for row in g], "words": sorted(ws), "dirs": nd}

def mm_reply(st, payload):
    """Code breaker: a wrong guess is answered with the hints (server keeps the secret)."""
    fb = mm_fb(st["code"], payload.get("guess"))
    if fb is None: return {"ok": False, "why": "payload"}
    if fb[0] != len(st["code"]): return {"ok": False, "why": "fb", "fb": fb}
    return None

# --- Rush Hour (6x6) and Sudoku 9x9 ---
# Puzzles were solved with a computer search; the number before | is the fewest slides needed (shown to the team as "Best possible").
# Each car is: row, column, length, h(orizontal) or v(ertical). The first car is the red one (it leaves through the right side of row 3).
RUSH = {
        "easy": ["5|232h;102h;412v;422h;442h;542h;042h;122v;002h;132h;252v;302v;022h",
                  "5|222h;542h;243v;113h;522h;042h;402h;253v;322v;012h;202v",
                  "4|202h;242v;232v;412h;502h;022v;132h;352v;432v",
                  "4|222h;022h;342v;122h;312h;053v;412h;522h;542h",
                  "5|232h;342v;332v;512h;132h;022v;322v;053v;003v;402v;012v;212v;533h",
                  "4|202h;412v;022h;103h;332h;352v;142v;052v;312h;432v;442v;303v;132v",
                  "4|202h;032v;332h;533h;142v;223v;103h;252v;402h",
                  "4|212h;302h;022v;152v;523h;242v;232v;322v;132h;502h;102h;352v",
                  "5|222h;542h;103v;042h;323h;022v;153v;403h;032v;113v;522h;002h;442h",
                  "7|202h;222v;333h;523h;422h;002h;033h;102h;132v;142h;412v;302v;022v",
                  "6|212h;312v;022v;442h;053v;042v;402v;332h;422h;522h",
                  "6|222h;422h;143v;532h;032h;202v;312v;012h;442h"],
        "medium": ["12|202h;023v;142v;133v;532h;512h;452v;002v;312h;342h;042h",
                  "9|202h;342h;302h;522h;142h;022v;412v;002v;452v;233v",
                  "9|202h;313h;012h;532h;442h;102h;122v;132h;252v;412h;502h;042h;242v;302v",
                  "10|222h;302h;003h;032h;122h;432v;422v;053v;542h;142v;322h",
                  "9|202h;312v;512h;422h;103h;012h;252v;442h;032v;043v;052v;532h",
                  "13|222h;502h;322v;032v;332h;302v;432h;012h;352v;003v;142v;042h;312v",
                  "11|212h;312v;532h;022h;152v;002v;342h;132v;422v;043v;012v;402v;432h",
                  "10|202h;352v;123v;032h;512h;302v;142h;012v;432h;132v;242v",
                  "10|202h;142v;523h;412h;332h;012v;042h;002v;153v;123v",
                  "13|202h;342v;122v;033v;012v;352v;312v;322h;422h;402v;142h;042h;532h;002v",
                  "10|202h;132h;312v;243v;012h;102h;432v;322v;542h;232v;032h;512h;152v;402v",
                  "10|202h;132v;532h;002h;302v;342v;352v;033h;413h;502h;313h;152v;022v"],
        "hard": ["16|202h;432v;032v;503h;312v;323h;153v;042v;542h;442h;122v;002h",
                  "17|202h;433h;342h;152v;232v;132h;312h;022v;422v;302v;502h;032h",
                  "19|222h;142v;342h;432h;502h;112h;102v;002h;422v;023h;212v;152v;322h",
                  "18|202h;353v;122h;412h;242v;302h;023h;402v;052v;333v;512h;012v;442v;222v",
                  "15|212h;332v;312h;033h;252v;102h;422v;123h;532h;402h;243v;452v;202v",
                  "21|222h;302v;013h;452v;412v;422v;242v;252v;042v;432h;122h",
                  "19|202h;153v;132v;312h;022h;002h;542h;432v;332h;042v;422v;303v",
                  "19|202h;222v;142v;422v;103h;402v;052v;432h;302h;452v;002h",
                  "18|202h;432v;122h;143v;402h;042h;002v;252v;322h;442h;012h;512h;542h;302h",
                  "18|202h;412h;402v;532h;332v;132h;352v;022h;042h;122v;012v;152v;342v",
                  "18|202h;152v;352v;302v;422h;033h;123v;232v;132h;342v;002v;012v",
                  "21|212h;422v;052v;022v;412v;432h;042v;102v;133v;002h;532h;252v"]}

def gen_rush(level):
    par, cs = random.choice(RUSH[level]).split("|")
    return {"cars": [[int(x[0]), int(x[1]), int(x[2]), x[3] == "h"] for x in cs.split(";")], "par": int(par)}

def rush_ok(cars, moves):
    """Replay the slides [car, new position]; true when the red car reaches the exit."""
    cars = [list(c) for c in cars]
    for i, p in moves[:500]:
        if not isinstance(i, int) or not isinstance(p, int) or not 0 <= i < len(cars): return False
        r, c, L, h = cars[i]; pos = c if h else r
        if p == pos or not 0 <= p <= 6 - L: return False
        occ = set()
        for j, (r2, c2, L2, h2) in enumerate(cars):
            if j != i: occ |= {(r2, c2 + k) if h2 else (r2 + k, c2) for k in range(L2)}
        lo, hi = min(pos, p), max(pos, p) + L
        if any(((r, k) if h else (k, c)) in occ for k in range(lo, hi)): return False
        cars[i][1 if h else 0] = p
    return cars[0][1] == 4

def gen_sudoku9(level):
    d = random.sample(range(1, 10), 9)
    perm = lambda: [b * 3 + x for b in random.sample(range(3), 3) for x in random.sample(range(3), 3)]
    rows, cols = perm(), perm()
    flat = [d[(3 * (r % 3) + r // 3 + c) % 9] for r in rows for c in cols]
    hide = set(random.sample(range(81), {"easy": 34, "medium": 44, "hard": 52}[level]))
    return [0 if i in hide else v for i, v in enumerate(flat)]

def gen_game(kind, level="medium"):
    if level not in LEVELS: level = "medium"
    st = {"level": level}
    if kind == "sliding":                       # easy/medium 3x3 (fewer/more scrambling moves), hard 4x4. Always solvable.
        n = 4 if level == "hard" else 3
        t, z, last = list(range(1, n * n)) + [0], n * n - 1, -1
        for _ in range({"easy": 20, "medium": 60, "hard": 120}[level]):
            r, c = divmod(z, n)
            opts = [nr * n + nc for nr, nc in ((r+1, c), (r-1, c), (r, c+1), (r, c-1)) if 0 <= nr < n and 0 <= nc < n and nr * n + nc != last]
            m = random.choice(opts); t[z], t[m] = t[m], 0; last, z = z, m
        st["tiles"] = t
    elif kind == "cipher":
        st.update(plain=random.choice(PHRASES[level]), shift=random.randint(*SHIFTS[level]))
    elif kind == "sudoku":                      # 4x4, easy 6 / medium 9 / hard 12 empty cells
        base = [[1, 2, 3, 4], [3, 4, 1, 2], [2, 1, 4, 3], [4, 3, 2, 1]]
        d = random.sample([1, 2, 3, 4], 4)
        rows = random.sample([0, 1], 2) + random.sample([2, 3], 2)
        cols = random.sample([0, 1], 2) + random.sample([2, 3], 2)
        flat = [d[base[r][c] - 1] for r in rows for c in cols]
        hide = set(random.sample(range(16), {"easy": 6, "medium": 9, "hard": 12}[level]))
        st["puzzle"] = [0 if i in hide else v for i, v in enumerate(flat)]
    elif kind == "memory":                      # easy 4 / medium 6 / hard 8 pairs
        cards = EMOJI[:{"easy": 4, "medium": 6, "hard": 8}[level]] * 2
        random.shuffle(cards); st["cards"] = cards
    elif kind == "lights":                      # easy 3x3, medium 4x4, hard 5x5; random presses on a solved board (always solvable)
        n = {"easy": 3, "medium": 4, "hard": 5}[level]
        while True:
            b = [0] * (n * n)
            for i in random.sample(range(n * n), {"easy": 3, "medium": 5, "hard": 9}[level]): toggle(b, n, i)
            if sum(b) >= 3: break
        st["board"] = b
    elif kind == "scramble":                    # easy 3 / medium 4 / hard 5 words
        ws = random.sample(WORDS[level], COUNT[level]); sc = []
        for x in ws:
            l = list(x)
            for _ in range(30):
                random.shuffle(l)
                if "".join(l) != x: break
            sc.append("".join(l))
        st.update(words=ws, scr=sc)
    elif kind == "quiz":                        # easy 3 / medium 4 / hard 5 questions (all must be correct)
        items = []
        for qq, a, wr in random.sample(QUIZ[level], COUNT[level]):
            o = [a] + list(wr); random.shuffle(o); items.append({"q": qq, "o": o, "a": o.index(a)})
        st["qs"] = items
    elif kind == "pattern":                     # 3 patterns; they get harder with the level
        st["seqs"] = [gen_pattern(level) for _ in range(3)]
    elif kind == "rushhour":                    # easy / medium / hard = fewer / more slides needed
        st.update(gen_rush(level))
    elif kind == "sudoku9":                     # easy 34 / medium 44 / hard 52 empty cells
        st["puzzle"] = gen_sudoku9(level)
    elif kind == "nonogram":                    # easy 5x5 / medium 7x7 / hard 8x8
        _, st["rows"], st["cols"] = gen_nono(level)
    elif kind == "codebreaker":                 # easy 3 pegs / medium 4 pegs / hard 5 pegs with repeats
        L, nc, rep_ = CB[level]
        st.update(code=[random.randrange(nc) for _ in range(L)] if rep_ else random.sample(range(nc), L), nc=nc, rep=rep_)
    elif kind == "wordsearch":                  # easy 8x8 / medium 10x10 / hard 12x12
        st.update(gen_wordsearch(level))
    elif kind == "hanoi":                       # easy 3 / medium 4 / hard 5 disks
        st["n"] = {"easy": 3, "medium": 4, "hard": 5}[level]
    return st

def game_view(kind, st):
    if kind == "cipher":
        k = st["shift"]
        return {"shift": k, "cipher": "".join(chr((ord(ch) - 65 + k) % 26 + 65) if ch.isalpha() else ch for ch in st["plain"])}
    if kind == "rushhour": return {"cars": [{"r": c[0], "c": c[1], "len": c[2], "h": c[3]} for c in st["cars"]], "par": st["par"]}
    if kind == "sudoku9": return st["puzzle"]
    if kind == "nonogram": return {"rows": st["rows"], "cols": st["cols"]}
    if kind == "codebreaker": return {"len": len(st["code"]), "colors": st["nc"], "rep": st["rep"]}
    if kind == "wordsearch": return {"grid": st["grid"], "words": st["words"], "dirs": st["dirs"]}
    if kind == "scramble": return st["scr"]
    if kind == "quiz": return [{"q": x["q"], "o": x["o"]} for x in st["qs"]]
    if kind == "pattern": return [x["s"] for x in st["seqs"]]
    if kind == "memory": return st["cards"]
    if kind == "lights": return st["board"]
    if kind == "hanoi": return {"n": st["n"]}
    if kind == "sudoku": return st["puzzle"]
    return st["tiles"]

def check_game(kind, st, p):
    try:
        if kind == "rushhour":
            return rush_ok(st["cars"], p["moves"])
        if kind == "sudoku9":
            g = p["grid"]
            if len(g) != 81 or any(a and a != b for a, b in zip(st["puzzle"], g)): return False
            lines = [g[i * 9:i * 9 + 9] for i in range(9)] + [g[i::9] for i in range(9)] + \
                    [[g[(br * 3 + r) * 9 + bc * 3 + c] for r in range(3) for c in range(3)] for br in range(3) for bc in range(3)]
            return all(sorted(x) == list(range(1, 10)) for x in lines)
        if kind == "nonogram":
            n = len(st["rows"]); g = p["grid"]
            if len(g) != n * n or any(v not in (0, 1) for v in g): return False
            return [nono_line(g[i * n:(i + 1) * n]) for i in range(n)] == st["rows"] and [nono_line(g[i::n]) for i in range(n)] == st["cols"]
        if kind == "codebreaker":
            fb = mm_fb(st["code"], p["guess"]); return fb is not None and fb[0] == len(st["code"])
        if kind == "wordsearch":
            g, ws, n, got = st["grid"], st["words"], len(st["grid"]), set()
            for r1, c1, r2, c2 in p["found"][:60]:
                if not all(isinstance(x, int) and 0 <= x < n for x in (r1, c1, r2, c2)): return False
                if not (r1 == r2 or c1 == c2 or abs(r2 - r1) == abs(c2 - c1)): return False
                dr, dc, ln = (r2 > r1) - (r2 < r1), (c2 > c1) - (c2 < c1), max(abs(r2 - r1), abs(c2 - c1)) + 1
                t = "".join(g[r1 + dr * i][c1 + dc * i] for i in range(ln))
                got.add(t if t in ws else t[::-1])
            return got == set(ws)
        if kind == "scramble":
            return [str(x).strip().upper() for x in p["answers"]] == st["words"]
        if kind == "quiz":
            return list(p["picks"]) == [x["a"] for x in st["qs"]]
        if kind == "pattern":
            return [int(str(x).strip()) for x in p["answers"]] == [x["a"] for x in st["seqs"]]
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
    if not a: return {"error": "Game is not available right now."}, 403
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
    if not a: return {"ok": False, "why": "session"}, 403           # team/code/step does not match (or this step is already done)
    team, pend = a
    kind, level = game_cfg(pend)
    row = q("select state, kind from games where team_id=? and pos=?", (team["id"], pend["pos"]), one=True)
    try: payload = json.loads(request.form["payload"])
    except Exception: return {"ok": False, "why": "payload"}
    if not row or row["kind"] != kind or json.loads(row["state"]).get("level") != level: return {"ok": False, "why": "reload"}   # game state lost/changed on the server
    if kind == "codebreaker":
        rp = mm_reply(json.loads(row["state"]), payload)
        if rp: return rp
    if not check_game(kind, json.loads(row["state"]), payload): return {"ok": False, "why": "wrong"}
    cur = content(pend["location_id"], pend["variant"])
    return {"ok": True, "code": (cur["code"] if cur else "")}

GAME_JS = r"""
(function(){
const G = window.GAME, box = document.getElementById('game');
const post = (u, extra) => { if (G.pv) u = '/admin/pv' + u; const f = new FormData(); if (G.pv) { f.append('kind', G.pv); f.append('level', G.level); } f.append('team_id', G.team); f.append('code', G.tcode); f.append('pos', G.pos);
  for (const k in (extra || {})) f.append(k, extra[k]); return fetch(u, {method: 'POST', body: f}).then(r => r.json()); };
const msg = t => { const m = box.querySelector('.gmsg'); if (m) m.textContent = t; };
function done(payload, cb){ post('/game/submit', {payload: JSON.stringify(payload)}).then(r => {
  if (!r.ok && r.why === 'fb' && cb) return cb(r);
  if (!r.ok) { if (r.why === 'reload') { msg('⚠️ The game was refreshed on the server. Loading a new one...'); return setTimeout(() => location.reload(), 1200); } if (r.why === 'session') return msg('⚠️ Session problem: scan the QR again and select your team (and code) once more.'); return msg('❌ Not correct yet, please try again.'); }
  box.innerHTML = '<div class="big">🎉 You won!' + (r.code ? '\nYour number code: <b style="font-size:34px">' + r.code + '</b>\nScan the QR of the next place and enter it there.' : '\nNow go to the next place.') + '</div>'; }); }
const GAMES = {
 rushhour(el, d){ const N = 6, CS = 48, COL = ['#2a9d8f', '#e9c46a', '#457b9d', '#8d6cab', '#f4a261', '#6a994e', '#bc6c25', '#4a6fa5', '#9c4f6b', '#5f7a61'];
  const start = d.cars.map(c => Object.assign({}, c)), cars = d.cars.map(c => Object.assign({}, c)), moves = [], back = []; let sel = -1, won = false;
  const posOf = c => c.h ? c.c : c.r, setPos = (c, p) => { if (c.h) c.c = p; else c.r = p; };
  const cells = (c, p) => { const o = []; for (let k = 0; k < c.len; k++) o.push(c.h ? c.r * N + p + k : (p + k) * N + c.c); return o; };
  const reach = i => { const c = cars[i], occ = new Set(); cars.forEach((x, j) => { if (j !== i) cells(x, posOf(x)).forEach(k => occ.add(k)); });
   const free = p => cells(c, p).every(k => !occ.has(k)), res = [], pos = posOf(c);
   for (let p = pos + 1; p + c.len <= N && free(p); p++) res.push(p);
   for (let p = pos - 1; p >= 0 && free(p); p--) res.push(p); return res; };
  const draw = () => {
   let h = '<p>Slide the cars out of the way so the <b style="color:#d62828">red car</b> can drive out through the green exit on the right. Tap a car, then tap a yellow arrow square to slide it there. A car moves only along its own direction (long side).</p>' +
    '<div style="text-align:center;margin:4px 0">Moves: <b>' + moves.length + '</b> &nbsp;|&nbsp; Best possible: <b>' + d.par + '</b></div>' +
    '<div id="rb" style="position:relative;width:' + N * CS + 'px;height:' + N * CS + 'px;margin:10px auto;background-color:#e9e6dc;background-image:linear-gradient(#cfc9b8 1px,transparent 1px),linear-gradient(90deg,#cfc9b8 1px,transparent 1px);background-size:' + CS + 'px ' + CS + 'px;border:4px solid #2b2d42;border-radius:8px">' +
    '<div style="position:absolute;right:-12px;top:' + 2 * CS + 'px;width:12px;height:' + CS + 'px;background:#1e8449"></div>';
   cars.forEach((c, i) => { h += '<div data-car="' + i + '" style="position:absolute;left:' + (c.c * CS + 3) + 'px;top:' + (c.r * CS + 3) + 'px;width:' + ((c.h ? c.len : 1) * CS - 6) + 'px;height:' + ((c.h ? 1 : c.len) * CS - 6) +
     'px;border-radius:10px;background:' + (i === 0 ? '#d62828' : COL[i % COL.length]) + ';box-shadow:' + (i === sel ? '0 0 0 4px #ffd166' : '0 1px 3px #0006') + ';display:flex;align-items:center;justify-content:center;font-size:22px;cursor:pointer">' + (i === 0 ? '🚗' : '') + '</div>'; });
   if (sel >= 0) { const c = cars[sel], pos = posOf(c);
    reach(sel).forEach(p => { const k = p > pos ? p + c.len - 1 : p, r = c.h ? c.r : k, cc = c.h ? k : c.c;
     h += '<div data-p="' + p + '" style="position:absolute;left:' + cc * CS + 'px;top:' + r * CS + 'px;width:' + CS + 'px;height:' + CS + 'px;display:flex;align-items:center;justify-content:center;z-index:5;cursor:pointer">' +
      '<div style="width:32px;height:32px;border-radius:50%;background:#ffd166;color:#222;font-size:18px;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 4px #0005">' + (c.h ? (p > pos ? '▶' : '◀') : (p > pos ? '▼' : '▲')) + '</div></div>'; }); }
   el.innerHTML = h + '</div><div style="text-align:center"><button id="ru">↶ Undo</button> <button id="rr">↺ Restart</button></div>';
   el.querySelectorAll('[data-car]').forEach(b => b.onclick = () => { const i = +b.dataset.car; sel = sel === i ? -1 : i; draw(); });
   el.querySelectorAll('[data-p]').forEach(b => b.onclick = () => { const c = cars[sel]; back.push([sel, posOf(c)]); moves.push([sel, +b.dataset.p]); setPos(c, +b.dataset.p); draw();
    if (cars[0].c === 4 && !won) { won = true; setTimeout(() => done({moves: moves}), 300); } });
   document.getElementById('rb').onclick = e => { if (e.target.id === 'rb') { sel = -1; draw(); } };
   document.getElementById('ru').onclick = () => { if (!back.length) return; const b = back.pop(); moves.pop(); setPos(cars[b[0]], b[1]); draw(); };
   document.getElementById('rr').onclick = () => { start.forEach((c, i) => { cars[i] = Object.assign({}, c); }); moves.length = 0; back.length = 0; sel = -1; draw(); };
  };
  draw(); },
 sudoku9(el, p){
  let h = '<p>Fill the grid so every row, column and 3x3 block has the numbers 1 to 9 exactly once. A number turns <span class="err">red</span> when it clashes with another one.</p>' +
    '<div style="display:grid;grid-template-columns:repeat(9,1fr);width:min(100%,360px);margin:12px auto;border:3px solid #2b2d42;border-radius:6px;overflow:hidden;background:#fff">';
  p.forEach((x, i) => { const r = i / 9 | 0, c = i % 9, line = '3px solid #2b2d42', thin = '1px solid #c8c8c8';
    const bl = c === 0 ? 'none' : (c % 3 === 0 ? line : thin), bt = r === 0 ? 'none' : (r % 3 === 0 ? line : thin), col = x ? '#2b2d42' : '#1a73e8';
    h += '<input ' + (x ? 'value="' + x + '" disabled' : '') + ' maxlength="1" inputmode="numeric" autocomplete="off" style="box-sizing:border-box;width:100%;aspect-ratio:1/1;margin:0;padding:0;border:0;border-radius:0;border-left:' + bl + ';border-top:' + bt +
      ';text-align:center;font-size:20px;font-weight:' + (x ? 700 : 500) + ';color:' + col + ';-webkit-text-fill-color:' + col + ';background:' + (x ? '#e9e6dc' : '#fff') + ';opacity:1">'; });
  el.innerHTML = h + '</div><button id="s9" style="width:100%">Check</button>';
  const inp = [...el.querySelectorAll('input')];
  const mark = () => { const g = inp.map(x => +x.value || 0);
   inp.forEach((x, i) => { if (p[i]) return; const r = i / 9 | 0, c = i % 9; let bad = false;
    if (g[i]) for (let j = 0; j < 81; j++) { if (j === i || g[j] !== g[i]) continue; const r2 = j / 9 | 0, c2 = j % 9;
     if (r2 === r || c2 === c || ((r / 3 | 0) === (r2 / 3 | 0) && (c / 3 | 0) === (c2 / 3 | 0))) { bad = true; break; } }
    const col = bad ? '#d62828' : '#1a73e8'; x.style.color = col; x.style.webkitTextFillColor = col; x.style.background = bad ? '#fde8e8' : '#fff'; }); };
  inp.forEach(x => { x.oninput = () => { x.value = x.value.replace(/[^1-9]/g, '').slice(0, 1); mark(); }; });
  document.getElementById('s9').onclick = () => done({grid: inp.map(x => +x.value || 0)}); },
 nonogram(el, d){ const n = d.rows.length, sz = n <= 5 ? 44 : (n <= 7 ? 36 : 30), g = Array(n * n).fill(0); let mode = 1, sent = false;
  const line = a => { const o = []; let r = 0; a.forEach(v => { if (v === 1) r++; else if (r) { o.push(r); r = 0; } }); if (r) o.push(r); return o.length ? o : [0]; };
  const same = (a, b) => a.join() === b.join();
  const draw = () => {
   const rowOk = d.rows.map((c, i) => same(line(g.slice(i * n, i * n + n)), c)), colOk = d.cols.map((c, j) => same(line(g.filter((_, k) => k % n === j)), c));
   const tone = ok => 'color:' + (ok ? '#1e8449' : '#222') + ';font-weight:' + (ok ? 700 : 500) + ';';
   let h = '<p>Fill squares so that every row and column matches its numbers. A number is a run of filled squares, for example <b>3 1</b> means 3 filled, a gap, then 1 filled. Numbers turn <span class="ok">green</span> when that row or column is right. Use ✕ to note squares you know are empty.</p>' +
    '<div style="text-align:center;margin:6px 0"><button id="nf" style="' + (mode === 1 ? 'background:#e0a800;color:#222' : '') + '">⬛ Fill</button> <button id="nx" style="' + (mode === 2 ? 'background:#e0a800;color:#222' : '') + '">✕ Mark empty</button> <button id="nr">↺ Clear</button></div>' +
    '<div style="overflow-x:auto"><table style="border-collapse:collapse;margin:auto;width:auto"><tr><td></td>' + d.cols.map((c, j) => '<td style="vertical-align:bottom;text-align:center;font-size:13px;line-height:1.15;padding:2px;' + tone(colOk[j]) + '">' + c.join('<br>') + '</td>').join('') + '</tr>';
   for (let i = 0; i < n; i++) { h += '<tr><td style="text-align:right;white-space:nowrap;padding-right:6px;font-size:13px;' + tone(rowOk[i]) + '">' + d.rows[i].join(' ') + '</td>';
    for (let j = 0; j < n; j++) { const v = g[i * n + j];
     h += '<td style="padding:0"><button data-i="' + (i * n + j) + '" style="width:' + sz + 'px;height:' + sz + 'px;margin:0;padding:0;border:1px solid #888;border-radius:0;font-size:' + (sz * 0.5 | 0) + 'px;background:' + (v === 1 ? '#2b2d42' : '#fff') + ';color:#c0392b;' +
      ((j + 1) % 5 === 0 && j < n - 1 ? 'border-right:3px solid #2b2d42;' : '') + ((i + 1) % 5 === 0 && i < n - 1 ? 'border-bottom:3px solid #2b2d42;' : '') + '">' + (v === 2 ? '✕' : '') + '</button></td>'; }
    h += '</tr>'; }
   el.innerHTML = h + '</table></div>';
   document.getElementById('nf').onclick = () => { mode = 1; draw(); }; document.getElementById('nx').onclick = () => { mode = 2; draw(); };
   document.getElementById('nr').onclick = () => { g.fill(0); draw(); };
   el.querySelectorAll('[data-i]').forEach(b => b.onclick = () => { const i = +b.dataset.i; g[i] = g[i] === mode ? 0 : mode; draw(); });
   if (!sent && rowOk.every(x => x) && colOk.every(x => x)) { sent = true; done({grid: g.map(v => v === 1 ? 1 : 0)}); setTimeout(() => { sent = false; }, 2500); }
  };
  draw(); },
 codebreaker(el, d){ const COL = ['🔴', '🟡', '🟢', '🔵', '🟣', '🟠'].slice(0, d.colors), L = d.len, hist = []; let cur = [];
  const draw = () => {
   let h = '<p>Crack the secret colour code of <b>' + L + '</b> pegs (' + (d.rep ? 'a colour can appear more than once' : 'no colour repeats') + '). Tap colours to build a guess, then press <b>Check guess</b>. Each guess gets hints:<br><b>●</b> right colour in the right place &nbsp;&nbsp; <b>○</b> right colour in the wrong place</p>';
   h += hist.map((x, i) => '<div style="display:flex;align-items:center;justify-content:center;gap:6px;padding:3px 0"><span style="width:24px;color:#777">' + (i + 1) + '.</span>' +
     x.g.map(c => '<span style="font-size:26px">' + COL[c] + '</span>').join('') + '<span style="font-size:20px;letter-spacing:2px;min-width:96px;text-align:left;margin-left:10px">' +
     '●'.repeat(x.b) + '○'.repeat(x.w) + (x.b + x.w ? '' : '<span style="color:#999;font-size:14px;letter-spacing:0">no match</span>') + '</span></div>').join('');
   h += '<div class="card" style="text-align:center;margin:10px 0"><div style="margin-bottom:8px">Your guess: ' + Array.from({length: L}, (_, i) =>
     '<button data-s="' + i + '" style="width:48px;height:48px;font-size:26px;background:#fff;border:2px dashed #999;margin:2px;padding:0">' + (cur[i] !== undefined ? COL[cur[i]] : '') + '</button>').join('') + '</div>' +
     COL.map((c, i) => '<button data-c="' + i + '" style="font-size:26px;width:52px;height:52px;background:#fff;border:1px solid #bbb;margin:3px;padding:0">' + c + '</button>').join('') +
     '<div style="margin-top:8px"><button id="cg" style="width:60%">Check guess</button></div><small style="color:#777">Tap a box in your guess to remove that colour.</small></div>';
   el.innerHTML = h;
   el.querySelectorAll('[data-c]').forEach(b => b.onclick = () => { if (cur.length < L) { cur.push(+b.dataset.c); draw(); } });
   el.querySelectorAll('[data-s]').forEach(b => b.onclick = () => { cur.splice(+b.dataset.s, 1); draw(); });
   document.getElementById('cg').onclick = () => { if (cur.length < L) return msg('Fill all ' + L + ' places first.');
    const guess = cur.slice(); done({guess: guess}, r => { hist.push({g: guess, b: r.fb[0], w: r.fb[1]}); cur = []; draw(); msg(''); }); };
  };
  draw(); },
 wordsearch(el, d){ const n = d.grid.length, found = [], PAL = ['#a9dfbf', '#aed6f1', '#f9e79f', '#f5b7b1', '#d7bde2', '#fad7a0', '#a3e4d7', '#d5dbdb', '#f8c471']; let start = -1;
  const line = (a, b) => { const r1 = a / n | 0, c1 = a % n, r2 = b / n | 0, c2 = b % n;
   if (!(r1 === r2 || c1 === c2 || Math.abs(r2 - r1) === Math.abs(c2 - c1))) return null;
   const dr = Math.sign(r2 - r1), dc = Math.sign(c2 - c1), len = Math.max(Math.abs(r2 - r1), Math.abs(c2 - c1)) + 1, cells = [];
   for (let i = 0; i < len; i++) cells.push((r1 + dr * i) * n + c1 + dc * i); return cells; };
  const draw = () => {
   const col = {}; found.forEach((f, k) => f.cells.forEach(c => { col[c] = PAL[k % PAL.length]; }));
   let h = '<p>Find all the hidden words listed below. Words run across, down' + (d.dirs >= 3 ? ', diagonally' : '') + (d.dirs === 8 ? ' and also backwards' : '') + '. Tap the <b>first</b> letter of a word, then its <b>last</b> letter.</p>' +
    '<div style="display:grid;grid-template-columns:repeat(' + n + ',1fr);gap:2px;width:min(100%,' + (n * 34) + 'px);margin:8px auto">';
   for (let i = 0; i < n * n; i++) h += '<button data-i="' + i + '" style="padding:0;margin:0;aspect-ratio:1/1;font-size:' + (n > 10 ? 14 : 17) + 'px;font-weight:700;border:1px solid #ccc;border-radius:4px;color:#222;background:' + (i === start ? '#ffd166' : (col[i] || '#fff')) + '">' + d.grid[i / n | 0][i % n] + '</button>';
   h += '</div><div style="display:flex;flex-wrap:wrap;gap:6px;justify-content:center">' + d.words.map(w => '<span style="padding:3px 10px;border-radius:12px;font-weight:600;background:' + (found.some(f => f.w === w) ? '#a9dfbf;text-decoration:line-through' : '#eee') + '">' + w + '</span>').join('') +
    '</div><p style="text-align:center">' + found.length + ' of ' + d.words.length + ' found</p>';
   el.innerHTML = h;
   el.querySelectorAll('[data-i]').forEach(b => b.onclick = () => { const i = +b.dataset.i; msg('');
    if (start < 0) { start = i; return draw(); }
    if (i === start) { start = -1; return draw(); }
    const a = start, cells = line(a, i); start = -1;
    if (!cells) { draw(); return msg('Those two letters are not in a straight line. Try again.'); }
    const s = cells.map(c => d.grid[c / n | 0][c % n]).join(''), r = s.split('').reverse().join('');
    const w = d.words.includes(s) ? s : (d.words.includes(r) ? r : null);
    if (!w || found.some(f => f.w === w)) { draw(); return msg('That is not one of the words. Try again.'); }
    found.push({w: w, cells: cells, a: [a / n | 0, a % n, i / n | 0, i % n]}); draw();
    if (found.length === d.words.length) done({found: found.map(f => f.a)}); }); };
  draw(); },
 scramble(el, ws){ el.innerHTML = '<p>Unscramble each word (all are engineering words).</p>' +
   ws.map((x, i) => '<div class="big" style="text-align:center;letter-spacing:6px;margin:6px 0">' + x + '</div><input data-i="' + i + '" style="width:100%;text-transform:uppercase" autocomplete="off" autocapitalize="characters">').join('') +
   '<button id="wb" style="width:100%">Check</button>';
  document.getElementById('wb').onclick = () => done({answers: [...el.querySelectorAll('input')].map(x => x.value)}); },
 quiz(el, qs){ el.innerHTML = '<p>Answer every question. All answers must be correct.</p>' +
   qs.map((x, i) => '<div class="card" style="margin:8px 0"><b>' + (i + 1) + '. ' + x.q + '</b>' + x.o.map((o, j) => '<label style="display:block;padding:6px 0"><input type="radio" name="q' + i + '" value="' + j + '"> ' + o + '</label>').join('') + '</div>').join('') +
   '<button id="qb" style="width:100%">Submit answers</button>';
  document.getElementById('qb').onclick = () => { const p = qs.map((_, i) => { const r = el.querySelector('input[name=q' + i + ']:checked'); return r ? +r.value : -1; });
   if (p.includes(-1)) return msg('Please answer every question.'); done({picks: p}); }; },
 pattern(el, ps){ el.innerHTML = '<p>Find the next number in each pattern.</p>' +
   ps.map((x, i) => '<div class="big" style="text-align:center;margin:6px 0">' + x.join(',  ') + ',  ?</div><input data-i="' + i + '" inputmode="numeric" style="width:100%" autocomplete="off">').join('') +
   '<button id="pb" style="width:100%">Check</button>';
  document.getElementById('pb').onclick = () => done({answers: [...el.querySelectorAll('input')].map(x => x.value)}); },
 sliding(el, t){ const moves = [], n = Math.round(Math.sqrt(t.length)), goal = t.map((_, i) => (i + 1) % t.length).join(), sz = n > 3 ? 68 : 80;
   const draw = () => { el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(' + n + ',' + sz + 'px);gap:6px;justify-content:center">' +
     t.map((v, i) => '<button data-i="' + i + '" style="height:' + sz + 'px;font-size:26px;' + (v ? '' : 'visibility:hidden') + '">' + v + '</button>').join('') +
     '</div><p>Arrange the tiles in order from 1 to ' + (t.length - 1) + ' (empty space at the bottom-right). Moves: ' + moves.length + '</p>';
    el.querySelectorAll('button').forEach(b => b.onclick = () => { const i = +b.dataset.i, z = t.indexOf(0);
     if (Math.abs((i / n | 0) - (z / n | 0)) + Math.abs(i % n - z % n) !== 1) return;
     t[z] = t[i]; t[i] = 0; moves.push(i); draw(); if (t.join() === goal) done({moves: moves}); }); };
   draw(); },
 memory(el, cards){ const moves = [], matched = new Set(); let open = [], lock = false;
  const draw = () => { el.innerHTML = '<p>Match the pairs: open two cards one after the other. If they match, they stay open. Moves: ' + (moves.length / 2) + '</p><div style="display:grid;grid-template-columns:repeat(4,64px);gap:8px;justify-content:center">' +
    cards.map((v, i) => '<button data-i="' + i + '" style="height:64px;font-size:30px;' + (matched.has(i) ? 'background:#a9dfbf;' : '') + '">' + ((matched.has(i) || open.includes(i)) ? v : '❓') + '</button>').join('') + '</div>';
   el.querySelectorAll('button').forEach(b => b.onclick = () => { if (lock) return; const i = +b.dataset.i; if (matched.has(i) || open.includes(i)) return;
    open.push(i);
    if (open.length < 2) return draw();
    const a = open[0], c = open[1]; moves.push(a, c);
    if (cards[a] === cards[c]) { matched.add(a); matched.add(c); open = []; draw(); if (matched.size === cards.length) done({moves: moves}); }
    else { draw(); lock = true; setTimeout(() => { open = []; lock = false; draw(); }, 800); } }); };
  draw(); },
 lights(el, L){ const presses = [], n = Math.round(Math.sqrt(L.length)), sz = Math.min(72, (280 / n) | 0);
   const draw = () => { el.innerHTML = '<p>Turn all the lights off (dark). Pressing a light toggles it and the ones above, below, left and right of it. Moves: ' + presses.length + '</p><div style="display:grid;grid-template-columns:repeat(' + n + ',' + sz + 'px);gap:6px;justify-content:center">' +
     L.map((v, i) => '<button data-i="' + i + '" style="height:' + sz + 'px;background:' + (v ? '#ffd166' : '#2b2d42') + '"></button>').join('') + '</div>';
    el.querySelectorAll('button').forEach(b => b.onclick = () => { const i = +b.dataset.i, r = i / n | 0, c = i % n;
     [[r, c], [r + 1, c], [r - 1, c], [r, c + 1], [r, c - 1]].forEach(p => { if (p[0] >= 0 && p[0] < n && p[1] >= 0 && p[1] < n) L[p[0] * n + p[1]] ^= 1; });
     presses.push(i); draw(); if (L.every(x => !x)) done({presses: presses}); }); };
   draw(); },
 cipher(el, s){ el.innerHTML = '<div class="big">' + s.cipher + '</div><p>Each letter is shifted forward in the alphabet by ' + s.shift + ' positions (for example, instead of A it is ' +
   String.fromCharCode(65 + s.shift) + '). Write the original sentence:</p><input id="ca" style="width:100%;text-transform:uppercase"><button id="cb">Check</button>';
  document.getElementById('cb').onclick = () => done({answer: document.getElementById('ca').value}); },
 sudoku(el, p){
  let h = '<p>Fill the grid so that every row, column and 2x2 block has the numbers 1 to 4 exactly once.</p>' +
    '<div style="display:grid;grid-template-columns:repeat(4,1fr);width:min(100%,288px);margin:12px auto;border:3px solid #2b2d42;border-radius:8px;overflow:hidden;background:#fff">';
  p.forEach((v, i) => { const r = i / 4 | 0, c = i % 4, line = '3px solid #2b2d42', thin = '1px solid #c8c8c8';
    const bl = c === 0 ? 'none' : (c === 2 ? line : thin), bt = r === 0 ? 'none' : (r === 2 ? line : thin), col = v ? '#2b2d42' : '#1a73e8';
    h += '<input ' + (v ? 'value="' + v + '" disabled' : '') + ' maxlength="1" inputmode="numeric" autocomplete="off" style="box-sizing:border-box;width:100%;aspect-ratio:1/1;margin:0;padding:0;border:0;border-radius:0;border-left:' + bl + ';border-top:' + bt +
      ';text-align:center;font-size:26px;font-weight:' + (v ? 700 : 500) + ';color:' + col + ';-webkit-text-fill-color:' + col + ';background:' + (v ? '#e9e6dc' : '#fff') + ';opacity:1">'; });
  el.innerHTML = h + '</div><button id="sb" style="width:100%">Check</button>';
  el.querySelectorAll('input').forEach(x => { x.oninput = () => { x.value = x.value.replace(/[^1-4]/g, '').slice(0, 1); }; });
  document.getElementById('sb').onclick = () => done({grid: [...el.querySelectorAll('input')].map(x => +x.value || 0)}); },
 hanoi(el, s){ const N = s.n, pegs = [Array.from({length: N}, (_, i) => N - i), [], []], moves = []; let sel = -1;
  const draw = () => { el.innerHTML = '<p>Move all the disks to the third peg. A larger disk cannot be placed on a smaller one. First pick a peg, then the one where you want to place it. Moves: ' + moves.length + '</p>' +
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
    try:
        p = json.loads(request.form["payload"])
        if k == "codebreaker" and k in PV:
            rp = mm_reply(PV[k], p)
            if rp: return rp
        ok = k in PV and check_game(k, PV[k], p)
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
        w("delete from games")                    # remove old game states; new ones are created per the new settings
        return redirect("/admin/games")
    manual = q("select v from settings where k='games_cfg'", one=True) is not None
    cur = game_steps(kmax)
    return page("""<h2>🎮 Games</h2>
    <div class="card"><h3>Which game at which step</h3>
    <p>At each step of a team's route (1 to {{ kmax }}) you can choose a game and its difficulty (easy / medium / hard). At a step marked "— none —" the plain puzzle and code are used.
    Currently: <b>{{ 'your setting' if manual else 'auto (automatic)' }}</b>.</p>
    <form method="post"><table><tr><th>Step</th><th>Game</th><th>Difficulty</th></tr>
    {% for p in range(1, kmax + 1) %}{% set c = cur.get(p) %}<tr><td>{{ p }}</td>
    <td><select name="k{{ p }}"><option value="">— none —</option>{% for k in kinds %}<option value="{{ k }}" {{ 'selected' if c and c[0] == k }}>{{ k }}</option>{% endfor %}</select></td>
    <td><select name="l{{ p }}">{% for l in levels %}<option value="{{ l }}" {{ 'selected' if (c[1] if c else 'medium') == l }}>{{ l }}</option>{% endfor %}</select></td></tr>{% endfor %}</table>
    <button>Save</button> <button name="auto" value="1" formnovalidate onclick="return confirm('Back to auto?')">Back to auto</button></form></div>
    <div class="card"><h3>Test a game</h3><p>Play any game right away. When you win, the code "1234 (preview)" will be shown.</p>
    <form method="get" onsubmit="location.href='/admin/games/'+this.k.value+'?level='+this.l.value; return false">
    <select name="k">{% for k in kinds %}<option>{{ k }}</option>{% endfor %}</select>
    <select name="l">{% for l in levels %}<option {{ 'selected' if l == 'medium' }}>{{ l }}</option>{% endfor %}</select> <button>Play</button></form></div>""",
    kinds=GAME_KINDS, levels=LEVELS, kmax=kmax, cur=cur, manual=manual)

@app.route("/admin/games/<kind>")
@admin
def game_preview(kind):
    if kind not in GAME_KINDS: abort(404)
    level = request.args.get("level", "medium")
    return page("""<h2>🎮 {{ kind }} ({{ level }}) — preview</h2><div id="game" class="card">Loading game...</div>
    <script>window.GAME={team:0,tcode:"",pos:0,pv:{{ kind|tojson }},level:{{ level|tojson }}};</script><script>{{ gjs|safe }}</script>
    <p><a href="/admin/games">← Games</a></p>""", kind=kind, level=level, gjs=GAME_JS)

# ---------- admin: round history (result save) + backup/restore ----------
def snapshot(label=""):
    """Saves the complete current result (each team's route + what happened when) in the 'rounds' table."""
    out = []
    for t in q("select * from teams order by name"):
        st = q("select s.pos, l.name lname, s.scan_ts, s.done_ts from steps s join locations l on l.id=s.location_id where s.team_id=? order by s.pos", (t["id"],))
        done = [x for x in st if x["done_ts"]]
        out.append({"team": t["name"], "code": t["code"], "done": len(done), "total": len(st),
                    "finished": bool(st) and len(done) == len(st), "last": max((x["done_ts"] for x in done), default=""),
                    "steps": [[x["pos"], x["lname"], x["scan_ts"] or "", x["done_ts"] or ""] for x in st]})
    if not any(t["done"] or any(x[2] for x in t["steps"]) for t in out): return None      # nothing played, so nothing to save
    cur = w(f"insert into rounds(ts,label,data) values({NOW},?,?)", (label or "Round", json.dumps(out, ensure_ascii=False)))
    return cur

def rank(data):
    return sorted(data, key=lambda t: (not t["finished"], -t["done"], t["last"] or "9"))

@app.route("/admin/history")
@admin
def history_page():
    rows = q("select id, ts, label, data from rounds order by id desc")
    rows = [{"id": r["id"], "ts": r["ts"], "label": r["label"], "n": len(json.loads(r["data"])),
             "win": (rank(json.loads(r["data"])) or [{}])[0].get("team", "-")} for r in rows]
    return page("""<h2>📜 Past rounds</h2><div class="card">
    <form method="post" action="/admin/history/save">Save the current result (without resetting): <input name="label" placeholder="Name, e.g. Round 1" style="width:180px"> <button>💾 Save</button></form></div>
    <div class="card">{% if not rows %}<p>No round has been saved yet. When you press "New round", the old result is saved here automatically.</p>{% else %}
    <table><tr><th>Round</th><th>When</th><th>Teams</th><th>First</th><th></th></tr>
    {% for r in rows %}<tr><td>{{ r.label }}</td><td>{{ r.ts }}</td><td>{{ r.n }}</td><td>{{ r.win }}</td>
    <td><a href="/admin/history/{{ r.id }}">View</a> | <a href="/admin/history/{{ r.id }}.csv">CSV</a></td></tr>{% endfor %}</table>{% endif %}</div>
    <div class="card"><h3>Backup / Restore (clue, puzzle, teams, locations)</h3>
    <p><a href="/admin/backup.json"><button type="button">⬇️ Backup download</button></a></p>
    <form method="post" action="/admin/restore" enctype="multipart/form-data" onsubmit="return confirm('The existing locations, puzzles and teams will be removed and replaced by those in the backup. Are you sure?')">
    <input type="file" name="f" accept=".json" required> <button class="del">⬆️ Restore</button></form></div>""", rows=rows)

@app.post("/admin/history/save")
@admin
def history_save():
    snapshot(request.form.get("label", "").strip())
    return redirect("/admin/history")

def _round(rid):
    r = q("select * from rounds where id=?", (rid,), one=True)
    if not r: abort(404)
    return r, rank(json.loads(r["data"]))

@app.route("/admin/history/<int:rid>")
@admin
def history_view(rid):
    r, data = _round(rid)
    return page("""<h2>📜 {{ r.label }} <small>({{ r.ts }})</small></h2><div class="card"><table>
    <tr><th>#</th><th>Team</th><th>Progress</th><th>Last time</th><th>Route (✅ = complete)</th></tr>
    {% for t in data %}<tr><td>{{ loop.index }}</td><td>{{ t.team }} {% if t.finished %}🏁{% endif %}</td><td>{{ t.done }}/{{ t.total }}</td><td>{{ t.last or '-' }}</td>
    <td>{% for s in t.steps %}{{ '✅' if s[3] else ('⏳' if s[2] else '·') }}{{ s[1] }}{% if not loop.last %} → {% endif %}{% endfor %}</td></tr>{% endfor %}</table></div>
    <p><a href="/admin/history">← all rounds</a> | <a href="/admin/history/{{ r.id }}.csv">CSV download</a></p>""", r=r, data=data)

@app.route("/admin/history/<int:rid>.csv")
@admin
def history_csv(rid):
    import csv, io
    r, data = _round(rid)
    buf = io.StringIO(); cw = csv.writer(buf)
    cw.writerow(["Rank", "Team", "Code", "Completed", "Total", "Finish", "Last time", "Route"])
    for i, t in enumerate(data, 1):
        cw.writerow([i, t["team"], t["code"], t["done"], t["total"], "yes" if t["finished"] else "no", t["last"],
                     " -> ".join(f"{s[1]} ({s[3] or 'pending'})" for s in t["steps"])])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=round_{rid}.csv"})

@app.route("/admin/backup.json")
@admin
def backup():
    d = {"teams": [dict(r) for r in q("select * from teams")], "locations": [dict(r) for r in q("select * from locations")],
         "riddles": [dict(r) for r in q("select * from riddles")], "settings": [dict(r) for r in q("select * from settings where k not in ('admin_pw','helper_pw','pw_pending','pw_otp_log')")]}
    return Response(json.dumps(d, ensure_ascii=False, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=hunt_backup.json"})

@app.post("/admin/restore")
@admin
def restore():
    try: d = json.load(request.files["f"].stream)
    except Exception: return page("<div class='card'>❌ This does not look like a valid backup file.</div><p><a href='/admin/history'>← back</a></p>")
    snapshot("Before restore")
    for t in ("games", "steps", "teams", "riddles", "locations"): w(f"delete from {t}")
    for r in d.get("teams", []): w("insert into teams(id,name,code) values(?,?,?)", (r["id"], r["name"], r["code"]))
    for r in d.get("locations", []): w("insert into locations(id,name,slug,kind) values(?,?,?,?)", (r["id"], r["name"], r["slug"], r.get("kind", "normal")))
    for r in d.get("riddles", []): w("insert into riddles(location_id,variant,clue,puzzle,code) values(?,?,?,?,?)", (r["location_id"], r["variant"], r.get("clue", ""), r.get("puzzle", ""), r.get("code", "")))
    for r in d.get("settings", []):
        if r["k"] not in ("admin_pw", "helper_pw", "pw_pending", "pw_otp_log"): w("insert or replace into settings values(?,?)", (r["k"], r["v"]))
    gen_routes(db())
    return redirect("/admin")

# ---------- admin: refresh / reset (to reuse the hunt) ----------
@app.route("/admin/reset", methods=["GET", "POST"])
@admin
def reset_page():
    if request.method == "POST":
        act = request.form.get("act")
        if act in ("round", "all"): snapshot(request.form.get("label", "").strip())     # save the result before resetting
        if act == "round":                       # teams, locations, puzzles, settings remain; only progress + routes are new
            w("delete from games")
            gen_routes(db())
        elif act == "all":                       # remove the teams too (locations, puzzles, settings remain)
            w("delete from games"); w("delete from steps"); w("delete from teams")
        return redirect("/admin/reset?done=" + (act or ""))
    teams = q("select count(*) from teams", one=True)[0]
    prog = q("select count(*) from steps where done_ts is not null", one=True)[0]
    return page("""<h2>🔄 Refresh / Reset</h2>
    {% if done %}<div class="card" style="background:#d4edda">✅ Done.</div>{% endif %}
    <div class="card"><p>Currently: <b>{{ teams }}</b> teams, <b>{{ prog }}</b> checkpoints completed.</p>
    <h3>1. New round (this is what you usually need)</h3>
    <p>All teams' progress and the leaderboard are reset to zero, and each team gets a new random route.
    Teams, locations, clues/puzzles/codes, the games setting and the number of routes stay as they are.</p>
    <form method="post" onsubmit="return confirm('The progress of all teams will be erased. Are you sure?')">
    <input type="hidden" name="act" value="round"><input name="label" placeholder="Name of this round (e.g. Round 1)" style="width:230px"> <button class="del">🔄 Start new round</button></form>
    <p>Before the reset, the result of the old round is saved automatically: <a href="/admin/history">📜 Past rounds</a></p></div>
    <div class="card"><h3>2. Also remove the teams</h3>
    <p>Along with the round reset, all teams are also deleted so that you can create new teams. Locations and puzzles stay.</p>
    <form method="post" onsubmit="return confirm('All teams will be deleted. Are you sure?')">
    <input type="hidden" name="act" value="all"><input type="hidden" name="label" value="Before removing teams"><button class="del">🗑️ Remove teams + progress</button></form></div>""",
    teams=teams, prog=prog, done=request.args.get("done"))

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
            msg, team = "❌ Wrong team code.", None
        else:
            steps = q(STEPS, (team["id"],))
            pend = next((s for s in steps if not s["done_ts"]), None)
            ans = request.form.get("answer")
            if not steps: msg = "Your route is not set; please contact the organizer."
            elif pend is None: res["finished"] = True
            elif loc["kind"] == "start": res.update(show(pend)); res["first"] = True
            elif pend["location_id"] == loc["id"]:
                tp = (team["id"], pend["pos"])
                w(f"update steps set scan_ts=coalesce(scan_ts,{NOW}) where team_id=? and pos=?", tp)
                cur = content(pend["location_id"], pend["variant"])
                need = (cur["code"] if cur else "").strip().replace(" ", "").lower()
                if need and ans is None: res["gate"] = True
                elif need and ans.strip().replace(" ", "").lower() != need:
                    msg, res["gate"] = "❌ Wrong code, look at the puzzle again and try once more.", True
                else:
                    w(f"update steps set done_ts={NOW} where team_id=? and pos=?", tp)
                    nxt = steps[pend["pos"]] if pend["pos"] < len(steps) else None
                    if nxt: res.update(show(nxt))
                    else: res["finished"] = True
            elif any(s["location_id"] == loc["id"] and s["done_ts"] for s in steps):
                res.update(show(pend)); res["again"] = True
            else: msg = "⚠️ This is not your next checkpoint. Read your previous clue again."
    return page("""<h2>📍 {{ loc.name }}</h2>
    {% if res.get('finished') %}<div class="big">🎉 Congratulations {{ team.name }}! You have solved all the puzzles, you are at the finish line!</div>
    {% elif res.get('gate') %}{% if msg %}<p class="err">{{ msg }}</p>{% else %}<p class="ok">✅ You've reached the right place, {{ team.name }}!</p>{% endif %}
      <form method="post" class="card"><input type="hidden" name="team_id" value="{{ team.id }}">
      <input type="hidden" name="code" value="{{ tcode }}">
      <label><b>Enter the number code you got by solving the previous puzzle here:</b></label><br>
      <input name="answer" required autocomplete="off" inputmode="numeric" placeholder="Number code" style="width:100%;font-size:20px">
      <button style="width:100%">Unlock</button></form>
    {% elif 'clue' in res %}<p class="ok">{{ '🔁 Your current clue and puzzle:' if (res.get('again') or res.get('first')) else '✅ Code correct! Here are your next clue and puzzle:' }}</p>
      {% if res.clue %}<div class="big">📍 {{ res.clue }}</div>{% endif %}
      {% if res.get('game') %}<div class="big">🎮 This time you have to play a game! If you win, you will get a number code.</div>
      <div id="game" class="card">Loading game...</div>
      <script>window.GAME={team:{{ team.id }},tcode:{{ tcode|tojson }},pos:{{ res.pos }}};</script><script>{{ gjs|safe }}</script>
      {% elif res.puzzle %}<div class="big">🧩 {{ res.puzzle }}</div>
      <p>Solve the puzzle, then scan the QR of the next place and enter the number code you get there.</p>{% endif %}
    {% else %}{% if msg %}<p class="err">{{ msg }}</p>{% endif %}
    <form method="post"><p><b>Click on your team's name</b></p>
    {% if rc %}<input name="code" placeholder="Team code" required autocomplete="off" style="text-transform:uppercase;width:100%"><br>{% endif %}
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-top:8px">
    {% for t in teams %}<button name="team_id" value="{{ t.id }}" style="padding:14px 6px;font-size:16px">{{ t.name }}</button>{% endfor %}
    </div></form>{% endif %}""", loc=loc, teams=teams, msg=msg, res=res, team=team,
    tcode=request.form.get("code", "").strip().upper(), rc=req_code(), gjs=GAME_JS)

# ---------- admin ----------
# Two kinds of login: the OWNER password (ADMIN_PASS, or the changed one) and a HELPER password for volunteers.
# Both open the admin panel. Changing either password needs a one-time code (OTP) that is sent ONLY to the owner's phone.
OTP_TTL, OTP_TRIES, OTP_GAP, OTP_PER_HOUR = 300, 5, 60, 5

def owner_ok(p):
    r = q("select v from settings where k='admin_pw'", one=True)      # a changed password is stored (hashed) in the database
    if r: return check_password_hash(r["v"], p)
    return hmac.compare_digest(p.encode(), ADMIN_PASS.encode())       # otherwise the ADMIN_PASS environment variable

def helper_ok(p):
    r = q("select v from settings where k='helper_pw'", one=True)
    return bool(r) and check_password_hash(r["v"], p)

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    bad = False
    if request.method == "POST":
        p = request.form.get("p", "")
        role = "owner" if owner_ok(p) else ("helper" if helper_ok(p) else None)
        if role:
            session["admin"], session["role"] = True, role; return redirect("/admin")
        bad = True
    return page("""<form method="post" class="card"><h3>Admin login</h3>{% if bad %}<p class="err">❌ Wrong password.</p>{% endif %}
    <input type="password" name="p" placeholder="Password"><button>Login</button></form>""", bad=bad)

def owner_phone():
    return "".join(ch for ch in os.environ.get("OWNER_PHONE", "") if ch.isdigit())[-10:]

def sms_ready():
    return bool(os.environ.get("FAST2SMS_KEY")) and len(owner_phone()) == 10

def send_otp_sms(otp):
    """Sends the OTP to OWNER_PHONE with Fast2SMS (route 'otp'). Without FAST2SMS_KEY + OWNER_PHONE the OTP is only printed in the terminal.
    Returns (ok, 'sms' | 'console' | error text)."""
    if not sms_ready():
        print(f"\n[OTP] Admin password change OTP: {otp}   (SMS is not configured, so it is shown only here)\n", flush=True)
        return True, "console"
    body = urllib.parse.urlencode({"variables_values": otp, "route": "otp", "numbers": owner_phone()}).encode()
    req = urllib.request.Request("https://www.fast2sms.com/dev/bulkV2", data=body, method="POST",
                                 headers={"authorization": os.environ["FAST2SMS_KEY"], "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: res = json.loads(e.read().decode())
        except Exception: res = {}
    except Exception:
        return False, "The SMS could not be sent (no internet or the SMS service is down). Try again."
    if res.get("return") is True: return True, "sms"
    return False, "The SMS service refused the request: " + str(res.get("message", "unknown error"))[:150]

def otp_hash(otp): return hashlib.sha256((app.secret_key + otp).encode()).hexdigest()

def get_json(k, default):
    r = q("select v from settings where k=?", (k,), one=True)
    try: return json.loads(r["v"]) if r else default
    except Exception: return default

def put_json(k, v): w("insert or replace into settings values(?,?)", (k, json.dumps(v)))

@app.route("/admin/password", methods=["GET", "POST"])
@admin
def password_page():
    f, msg, good, now = request.form, None, False, time.time()
    pend = get_json("pw_pending", None)
    if pend and pend["exp"] < now: pend = None; w("delete from settings where k='pw_pending'")
    act = f.get("act") if request.method == "POST" else None
    if act == "send":
        which, new = f.get("which"), f.get("new", "")
        log = [t for t in get_json("pw_otp_log", []) if now - t < 3600]
        if which not in ("owner", "helper"): msg = "Choose which password you want to change."
        elif len(new) < 6: msg = "The new password must have at least 6 characters."
        elif new != f.get("again"): msg = "The two new passwords do not match."
        elif (which == "owner" and helper_ok(new)) or (which == "helper" and owner_ok(new)): msg = "The owner and helper passwords must be different."
        elif pend and now - pend["sent"] < OTP_GAP: msg = f"Please wait {int(OTP_GAP - (now - pend['sent'])) + 1} seconds before asking for another OTP."
        elif len(log) >= OTP_PER_HOUR: msg = "Too many OTP requests in the last hour. Please try again later."
        else:
            otp = f"{secrets.randbelow(10 ** 6):06d}"
            ok, how = send_otp_sms(otp)
            if not ok: msg = "❌ " + how
            else:
                pend = {"which": which, "pw": generate_password_hash(new), "otp": otp_hash(otp), "exp": now + OTP_TTL, "sent": now, "tries": 0, "by": session.get("role", "owner"), "how": how}
                put_json("pw_pending", pend); put_json("pw_otp_log", log + [now])
    elif act == "verify" and pend:
        if pend["tries"] >= OTP_TRIES:
            pend = None; w("delete from settings where k='pw_pending'"); msg = "Too many wrong OTPs. Please start again."
        elif hmac.compare_digest(otp_hash(f.get("otp", "").strip()), pend["otp"]):
            w("insert or replace into settings values(?,?)", ("admin_pw" if pend["which"] == "owner" else "helper_pw", pend["pw"]))
            w("delete from settings where k='pw_pending'")
            msg, good, pend = f"✅ The {pend['which']} password was changed. Use the new password the next time you log in.", True, None
        else:
            pend["tries"] += 1; put_json("pw_pending", pend); msg = f"❌ Wrong OTP ({OTP_TRIES - pend['tries']} tries left)."
    elif act == "cancel": pend = None; w("delete from settings where k='pw_pending'")
    ph = owner_phone()
    return page("""<h2>🔑 Admin passwords</h2>
    <div class="card"><p>You are logged in as: <b>{{ role }}</b>. Two logins work in this panel: the <b>owner</b> password and a <b>helper</b> password you can give to volunteers.
    To change either password, a one-time code (OTP) is sent <b>only to the owner's phone</b>{% if ph %} (xxxxxx{{ ph[-4:] }}){% endif %}. Without that OTP nothing changes, even if someone else is logged in.</p>
    <p>Helper password: <b>{{ 'set' if helper else 'not set' }}</b> &nbsp;|&nbsp; SMS: <b>{{ 'Fast2SMS is ready' if ready else 'not configured: the OTP is shown only in the terminal where the app runs' }}</b></p></div>
    {% if msg %}<p class="{{ 'ok' if good else 'err' }}">{{ msg }}</p>{% endif %}
    {% if pend %}<form method="post" class="card"><h3>Enter the OTP</h3>
    <p>{{ 'An OTP was sent by SMS to the owner\\'s phone' if pend.how == 'sms' else 'An OTP was printed in the terminal where the app runs' }} to change the <b>{{ pend.which }}</b> password. It is valid for 5 minutes.</p>
    <input name="otp" inputmode="numeric" maxlength="6" autocomplete="off" placeholder="6-digit OTP" required style="font-size:20px;letter-spacing:4px">
    <button name="act" value="verify">Confirm</button> <button name="act" value="cancel" formnovalidate class="del">Cancel</button></form>
    {% else %}<form method="post" class="card"><h3>Change a password</h3>
    <label><input type="radio" name="which" value="owner" checked> Owner password</label> &nbsp;
    <label><input type="radio" name="which" value="helper"> Helper password</label><br>
    <label>New password (at least 6 characters)</label><br><input type="password" name="new" required minlength="6" style="width:100%"><br>
    <label>New password again</label><br><input type="password" name="again" required style="width:100%"><br>
    <button name="act" value="send">Send OTP to the owner's phone</button></form>{% endif %}
    <div class="card"><small>Forgot the owner password? Stop the app and start it once with <code>RESET_ADMIN_PASS=1</code>
    (PowerShell: <code>$env:RESET_ADMIN_PASS="1"; python app.py</code>). Both passwords are cleared: the owner password goes back to ADMIN_PASS and the helper login is switched off.</small></div>
    <p><a href="/admin">← Dashboard</a></p>""", msg=msg, good=good, pend=pend, ph=ph, ready=sms_ready(), role=session.get("role", "owner"),
    helper=q("select 1 from settings where k='helper_pw'", one=True) is not None)

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
    <div class="card"><h3>Progress (who has reached where)</h3>
    <p>✅ code correct, moved on &nbsp; ⏳ arrived, code pending &nbsp; · not reached yet</p><div style="overflow-x:auto"><table>
    <tr><th>Team</th><th>Code</th><th>Done</th><th>Route</th><th></th></tr>
    {% for t in teams %}{% set rt = routes.get(t.id, []) %}<tr>
    <td><form method="post" action="/admin/team/{{ t.id }}/rename"><input name="name" value="{{ t.name }}" size="12"><button>Save</button></form></td>
    <td><code>{{ t.code }}</code></td><td>{{ rt|selectattr('done_ts')|list|length }}/{{ rt|length }}</td>
    <td>{% for s in rt %}{{ '✅' if s.done_ts else ('⏳' if s.scan_ts else '·') }}{{ s.lname }}{% if not loop.last %} → {% endif %}{% endfor %}</td>
    <td><a href="/admin/team/{{ t.id }}/route">Route</a>
    <form method="post" action="/admin/team/{{ t.id }}/delete" onsubmit="return confirm('Delete {{ t.name }}?')"><button class="del">Delete</button></form></td></tr>{% endfor %}</table></div></div>
    <div class="card"><h3>Add a team</h3><form method="post" action="/admin/team">
    <input name="name" placeholder="Team name" required><button>Add</button></form>
    <h3>Team code: {{ 'ON' if rc else 'OFF' }}</h3><form method="post" action="/admin/toggle_code">
    <button>{{ 'Turn OFF' if rc else 'Turn ON (name + code)' }}</button></form></div>
    <div class="card"><h3>Locations (clue / puzzle / code + QR)</h3><table>
    {% for l in locs %}<tr><td>{{ l.name }} <small>({{ l.kind }})</small></td><td><a href="/admin/loc/{{ l.id }}">Edit + QR</a></td>
    <td><form method="post" action="/admin/loc/{{ l.id }}/delete" onsubmit="return confirm('Delete?')"><button class="del">Delete</button></form></td></tr>{% endfor %}</table>
    <form method="post" action="/admin/loc"><input name="name" placeholder="Location name" required>
    <select name="kind"><option value="normal">Normal</option><option value="finish">Finish</option><option value="start">Start</option></select>
    <button>Add location</button></form>
    <p><small>After adding a new location, press "Regenerate routes" on the Routes page.</small></p></div>
    <div class="card"><a href="/admin/routes">🗺️ Routes</a> &nbsp;|&nbsp; <a href="/admin/games">🎮 Games</a> &nbsp;|&nbsp; <a href="/admin/reset">🔄 Refresh</a> &nbsp;|&nbsp; <a href="/admin/history">📜 History</a> &nbsp;|&nbsp; <a href="/admin/password">🔑 Password</a> &nbsp;|&nbsp; <a href="/admin/logout">Logout</a></div>""",
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
        db().commit(); fix_variants(db()); return redirect(f"/admin/loc/{i}")
    vs = q("select * from riddles where location_id=? order by variant", (i,))
    visits = q("select count(*) from steps where location_id=?", (i,), one=True)[0]
    url = request.host_url + "s/" + loc["slug"]
    return page("""<h2>{{ loc.name }} <small>({{ loc.kind }})</small></h2><div class="card" style="text-align:center">
    <div id="qr" style="display:inline-block"></div><p><code>{{ url }}</code></p>
    <button class="noprint" onclick="print()">Print QR</button></div>
    <form method="post" class="noprint"><div class="card"><label>Name</label><br><input name="name" value="{{ loc.name }}"></div>
    {% if loc.kind != 'start' %}<div class="card" style="background:{{ '#fde8e8' if visits > vs|length else '#e8f5e9' }}">{{ visits }} teams visit this place, you have {{ vs|length }} sets.
    {% if visits > vs|length %}<b>Add at least {{ visits - vs|length }} more set(s)</b>, otherwise some teams will get the same clue.{% else %}Every team gets a different set. ✅{% endif %}</div>
    <div class="card"><p><b>Clue + puzzle + code for this place.</b> The team sees the clue and puzzle at the previous place (or start).
    The team solves the puzzle and enters the resulting <b>number code</b> after scanning this place's QR. At some steps a game appears instead of the puzzle (set it from Admin → Games); the clue and code from here still apply then. Different team groups get different sets,
    so two teams going to the same place get different puzzles/codes (keep at least 2 sets).</p></div>
    {% for v in vs %}<div class="card"><h3>Set {{ v.variant + 1 }}</h3>
    <label>Clue (the way to this place)</label><br><textarea name="clue_{{ v.variant }}" rows="2" style="width:100%">{{ v.clue }}</textarea><br>
    <label>Puzzle (description of the jigsaw / word riddle)</label><br><textarea name="puzzle_{{ v.variant }}" rows="3" style="width:100%">{{ v.puzzle }}</textarea><br>
    <label>Number code (answer to the puzzle)</label> <input name="code_{{ v.variant }}" value="{{ v.code }}" size="10">
    <button class="del" name="act" value="del_{{ v.variant }}" onclick="return confirm('Delete this set?')">Delete set</button></div>{% endfor %}
    <button name="act" value="add">+ New set</button> {% endif %}<button name="act" value="save">Save</button></form>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <script>new QRCode(document.getElementById('qr'),{text:{{ url|tojson }},width:260,height:260});</script>""", loc=loc, vs=vs, url=url, visits=visits)

@app.route("/admin/routes", methods=["GET", "POST"])
@admin
def routes_page():
    nloc = q("select count(*) from locations where kind='normal'", one=True)[0]
    if request.method == "POST":
        try: k = max(1, min(int(request.form.get("per_team", "0")), nloc))
        except ValueError: k = nloc
        w("insert or replace into settings values('per_team',?)", (str(k),))
        gen_routes(db()); return redirect("/admin/routes")
    short = q("""select l.name, count(s.team_id) visits, (select count(*) from riddles r where r.location_id=l.id) sets from locations l
                 join steps s on s.location_id=l.id group by l.id having visits > sets order by l.name""")
    cur_k = per_team(db(), nloc)
    rows = {}
    for s in q("select s.team_id, s.pos, l.name lname from steps s join locations l on l.id=s.location_id order by team_id,pos"):
        rows.setdefault(s["team_id"], []).append(s["lname"])
    return page("""<h2>🗺️ Routes</h2><div class="card"><p>Each team gets a different order so that everyone doesn't crowd at the same place.</p>
    <form method="post" onsubmit="return confirm('All routes will be regenerated and progress will be reset. Are you sure?')">
    How many locations each team will visit (out of {{ nloc }} in total): <input name="per_team" type="number" min="1" max="{{ nloc }}" value="{{ cur_k }}" style="width:70px">
    <button class="del">Save and regenerate routes (progress reset)</button></form></div>
    {% if short %}<div class="card" style="background:#fde8e8"><b>⚠️ Not enough clue sets (some teams will get the same clue):</b><br>
    {% for x in short %}{{ x.name }}: {{ x.visits }} teams, only {{ x.sets }} sets (add {{ x.visits - x.sets }} more)<br>{% endfor %}</div>{% endif %}
    <form method="post" action="/admin/fix_variants" class="card">Different clue for every team (does not reset progress): <button>Fix clues now</button></form>
    <div class="card"><table>{% for t in teams %}<tr><td><a href="/admin/team/{{ t.id }}/route">{{ t.name }}</a></td>
    <td>{{ rows.get(t.id, [])|join(' → ') }}</td></tr>{% endfor %}</table></div>""", teams=q("select * from teams order by name"), rows=rows, nloc=nloc, cur_k=cur_k, short=short)

@app.post("/admin/fix_variants")
@admin
def fix_variants_route():
    fix_variants(db()); return redirect("/admin/routes")

@app.route("/admin/team/<int:i>/route", methods=["GET", "POST"])
@admin
def team_route(i):
    t = q("select * from teams where id=?", (i,), one=True) or abort(404)
    locs = q("select * from locations where kind!='start' order by id")
    n = len(locs)
    if request.method == "POST":
        w("delete from steps where team_id=?", (i,))
        for p in range(1, n + 1):
            if request.form.get(f"p{p}"): db().execute("insert into steps(team_id,pos,location_id,variant) values(?,?,?,?)", (i, p, request.form[f"p{p}"], pick_variant(db(), int(request.form[f"p{p}"]), i)))
        db().commit(); return redirect("/admin/routes")
    cur = {s["pos"]: s["location_id"] for s in q("select pos,location_id from steps where team_id=?", (i,))}
    return page("""<h2>Route: {{ t.name }}</h2><form method="post" class="card"><p>Saving will reset this team's progress.</p>
    {% for p in range(1, n + 1) %}Step {{ p }}: <select name="p{{ p }}"><option value="">--</option>
    {% for l in locs %}<option value="{{ l.id }}" {{ 'selected' if cur.get(p) == l.id }}>{{ l.name }}</option>{% endfor %}</select><br>{% endfor %}
    <button>Save route</button></form>""", t=t, locs=locs, n=n, cur=cur)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
