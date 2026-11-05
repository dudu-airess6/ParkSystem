#!/usr/bin/env python3
"""ParkSystem - Sistema de Gestão de Estacionamentos.

Servidor web + API em Python puro (sem instalar nada) com banco SQLite.
Uso:  python app.py   ->  abre http://localhost:8000
"""
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import webbrowser
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PARK_DB", os.path.join(BASE, "parksystem.db"))
PORT = int(os.environ.get("PORT", "8000"))

OP, ADM = "Operador de Pátio", "ADM / Gerente"
TIPOS = ("Carro", "Moto")
FORMAS = ("Dinheiro", "Cartão", "PIX")
PLANOS = ("Mensal", "Trimestral", "Anual")
OCORRENCIAS = ("Risco na pintura", "Amassado", "Objeto esquecido", "Outro")
SESS = {}  # token -> dados do usuário logado (memória)

SCHEMA = """
create table if not exists usuarios(
  id integer primary key, nome text not null, login text unique not null,
  salt text not null, senha text not null, perfil text not null);
create table if not exists vagas(
  id integer primary key, codigo text unique not null, tipo text not null,
  status text not null default 'livre');            -- livre | reservada | bloqueada
create table if not exists mensalistas(
  id integer primary key, nome text not null, cpf text unique not null, telefone text,
  placas text not null, plano text not null, vencimento text not null, ativo integer not null default 1);
create table if not exists config(chave text primary key, valor text not null);
create table if not exists tarifas(
  tipo text primary key, valor_hora real not null, valor_fracao real not null, valor_diaria real not null);
create table if not exists estadias(
  id integer primary key, ticket text unique not null, placa text not null, tipo text not null,
  vaga_id integer not null, entrada text not null, saida text, operador_id integer not null,
  valor real, forma_pgto text, mensalista_id integer, status text not null default 'ativa');
create table if not exists pagamentos(
  id integer primary key, estadia_id integer not null, valor real not null, forma text not null,
  status text not null, autorizacao text, data text not null);
create table if not exists ocorrencias(
  id integer primary key, estadia_id integer, placa text, tipo text not null,
  descricao text not null, data text not null, operador_id integer not null);
"""


# ----------------------------------------------------------------- utilidades
def agora():
    return datetime.now().replace(microsecond=0)


def hash_senha(senha, salt):
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt), 100_000).hex()


def norm_placa(p):
    return re.sub(r"[^A-Z0-9]", "", (p or "").upper())


def placa_ok(p):  # ABC1234 (antiga) ou ABC1D23 (Mercosul)
    return bool(re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9][0-9]{2}", p))


def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def conectar():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def iniciar_banco():
    db = conectar()
    db.executescript(SCHEMA)
    if not db.execute("select 1 from usuarios").fetchone():
        for nome, login, senha, perfil in (
            ("Operador Demo", "operador", "operador123", OP),
            ("Gerente Demo", "gerente", "gerente123", ADM),
        ):
            salt = secrets.token_hex(8)
            db.execute("insert into usuarios(nome,login,salt,senha,perfil) values(?,?,?,?,?)",
                       (nome, login, salt, hash_senha(senha, salt), perfil))
        for i in range(1, 19):
            st = "reservada" if i <= 3 else "bloqueada" if i == 18 else "livre"
            db.execute("insert into vagas(codigo,tipo,status) values(?,?,?)", ("A%02d" % i, "Carro", st))
        for i in range(1, 7):
            db.execute("insert into vagas(codigo,tipo,status) values(?,?,?)",
                       ("M%02d" % i, "Moto", "bloqueada" if i == 6 else "livre"))
        db.executemany("insert into config values(?,?)", [("tolerancia_min", "10"), ("fracao_min", "15")])
        db.executemany("insert into tarifas values(?,?,?,?)",
                       [("Carro", 8.0, 2.0, 40.0), ("Moto", 5.0, 1.0, 20.0)])
    db.commit()
    db.close()


# ----------------------------------------------------------------- regras de negócio
class ApiError(Exception):
    def __init__(self, code, msg, extra=None):
        self.code, self.msg, self.extra = code, msg, extra or {}


def achar_mensalista(db, placa):
    for m in db.execute("select * from mensalistas where ativo=1"):
        if placa in [norm_placa(x) for x in m["placas"].split(",")]:
            return m
    return None


def plano_valido(m):
    return m["vencimento"] >= date.today().isoformat()


def calcular(db, est, fim):
    """UC11 - Calcular Tarifa."""
    cfg = {r["chave"]: int(r["valor"]) for r in db.execute("select * from config")}
    tol, fr = cfg["tolerancia_min"], cfg["fracao_min"]
    t = db.execute("select * from tarifas where tipo=?", (est["tipo"],)).fetchone()
    minutos = max(0, int((fim - datetime.fromisoformat(est["entrada"])).total_seconds() // 60))
    mens = achar_mensalista(db, est["placa"])
    base = {"minutos": minutos, "mensalista": mens["nome"] if mens else None}
    if mens and plano_valido(mens):
        return dict(base, valor=0.0, detalhe=f"Mensalista com plano {mens['plano']} ativo: sem cobrança.")
    aviso = f"Plano vencido em {mens['vencimento']}: tarifa normal. " if mens else ""
    dias, resto = divmod(minutos, 1440)
    partes, parcela = [], 0.0
    if dias:
        partes.append(f"{dias} diária(s) × {brl(t['valor_diaria'])}")
    if resto > tol:
        if resto <= 60:
            parcela = t["valor_hora"]
            partes.append(f"1ª hora {brl(parcela)}")
        else:
            n = math.ceil((resto - 60) / fr)
            parcela = t["valor_hora"] + n * t["valor_fracao"]
            partes.append(f"1ª hora {brl(t['valor_hora'])} + {n} fração(ões) de {fr} min × {brl(t['valor_fracao'])}")
        if parcela > t["valor_diaria"]:
            parcela = t["valor_diaria"]
            partes.append(f"limitado à diária de {brl(parcela)}")
    valor = round(dias * t["valor_diaria"] + parcela, 2)
    if not partes:
        return dict(base, valor=0.0, detalhe=aviso + f"Dentro da tolerância de {tol} min: sem cobrança.")
    return dict(base, valor=valor, detalhe=aviso + "; ".join(partes) + ".")


def estadia_ativa(db, q):
    q = (q or "").strip().upper()
    est = db.execute("select * from estadias where ticket=? and status='ativa'", (q,)).fetchone()
    return est or db.execute("select * from estadias where placa=? and status='ativa'",
                             (norm_placa(q),)).fetchone()


def estadia_json(db, est, calc=True):
    v = db.execute("select codigo from vagas where id=?", (est["vaga_id"],)).fetchone()
    d = {"id": est["id"], "ticket": est["ticket"], "placa": est["placa"], "tipo": est["tipo"],
         "vaga": v["codigo"], "entrada": est["entrada"]}
    if calc:
        d.update(calcular(db, est, agora()))
    return d


# ----------------------------------------------------------------- rotas
ROUTES = []


def route(method, pattern, perfil=None):
    """perfil: None = público | 'any' = logado | OP | ADM."""
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), perfil, fn))
        return fn
    return deco


class Resp:
    def __init__(self, data, code=200, ctype="application/json; charset=utf-8", headers=None):
        self.data, self.code, self.ctype, self.headers = data, code, ctype, headers or []


# --- UC01 Autenticar usuário
@route("POST", "/api/login")
def login(c):
    u = c.db.execute("select * from usuarios where login=?", ((c.body.get("login") or "").strip().lower(),)).fetchone()
    senha = c.body.get("senha") or ""
    if not u or not hmac.compare_digest(u["senha"], hash_senha(senha, u["salt"])):
        raise ApiError(401, "Usuário ou senha incorretos.")
    tok = secrets.token_urlsafe(24)
    SESS[tok] = {"id": u["id"], "nome": u["nome"], "perfil": u["perfil"]}
    return Resp(SESS[tok], headers=[("Set-Cookie", f"sid={tok}; Path=/; HttpOnly; SameSite=Strict")])


@route("POST", "/api/logout", "any")
def logout(c):
    SESS.pop(c.sid, None)
    return {"ok": True}


@route("GET", "/api/me", "any")
def me(c):
    return c.user


# --- UC04 Visualizar mapa de vagas
@route("GET", "/api/mapa", OP)
def mapa(c):
    ativas = {e["vaga_id"]: e for e in c.db.execute("select * from estadias where status='ativa'")}
    vagas, resumo = [], {"livre": 0, "ocupada": 0, "reservada": 0, "bloqueada": 0}
    for v in c.db.execute("select * from vagas order by tipo, codigo"):
        e = ativas.get(v["id"])
        st = "ocupada" if e else v["status"]
        resumo[st] += 1
        vagas.append({"id": v["id"], "codigo": v["codigo"], "tipo": v["tipo"], "status": st,
                      "placa": e["placa"] if e else None, "entrada": e["entrada"] if e else None,
                      "ticket": e["ticket"] if e else None})
    return {"vagas": vagas, "resumo": resumo}


@route("GET", "/api/patio", OP)
def patio(c):
    rows = c.db.execute("select * from estadias where status='ativa' order by entrada desc").fetchall()
    return [estadia_json(c.db, e, calc=False) for e in rows]


# --- UC02 Registrar entrada
@route("POST", "/api/entrada", OP)
def entrada(c):
    db, b = c.db, c.body
    placa, tipo = norm_placa(b.get("placa")), b.get("tipo")
    if not placa_ok(placa):
        raise ApiError(400, "Placa inválida. Use o formato ABC1234 ou ABC1D23.")
    if tipo not in TIPOS:
        raise ApiError(400, "Escolha o tipo de veículo.")
    if db.execute("select 1 from estadias where placa=? and status='ativa'", (placa,)).fetchone():
        raise ApiError(409, "Este veículo já está no pátio.")
    mens = achar_mensalista(db, placa)
    mens = mens if mens and plano_valido(mens) else None
    ocupadas = {r[0] for r in db.execute("select vaga_id from estadias where status='ativa'")}
    if b.get("vaga_id"):
        v = db.execute("select * from vagas where id=?", (b["vaga_id"],)).fetchone()
        if not v or v["tipo"] != tipo or v["id"] in ocupadas or v["status"] == "bloqueada":
            raise ApiError(409, "Essa vaga não está disponível para este veículo. Escolha outra.")
        if v["status"] == "reservada" and not mens:
            raise ApiError(409, "Vaga reservada para mensalistas. Escolha outra.")
    else:
        st = ("reservada", "livre") if mens else ("livre",)
        cands = [v for v in db.execute("select * from vagas where tipo=? order by codigo", (tipo,))
                 if v["status"] in st and v["id"] not in ocupadas]
        if mens:
            cands.sort(key=lambda v: v["status"] != "reservada")
        if not cands:
            raise ApiError(409, "Pátio lotado: não há vagas disponíveis para este tipo de veículo.")
        v = cands[0]
    ticket = "PK-" + secrets.token_hex(3).upper()
    t = agora().isoformat()
    cur = db.execute("insert into estadias(ticket,placa,tipo,vaga_id,entrada,operador_id,mensalista_id) values(?,?,?,?,?,?,?)",
                     (ticket, placa, tipo, v["id"], t, c.user["id"], mens["id"] if mens else None))
    return {"id": cur.lastrowid, "ticket": ticket, "placa": placa, "tipo": tipo, "vaga": v["codigo"],
            "entrada": t, "mensalista": mens["nome"] if mens else None}


# --- UC03 Registrar saída (inclui UC11 e UC10)
@route("GET", "/api/saida/buscar", OP)
def saida_buscar(c):
    est = estadia_ativa(c.db, c.q.get("q"))
    if not est:
        raise ApiError(404, "Ticket ou placa não localizados no pátio. Escolha o veículo na lista abaixo.")
    return estadia_json(c.db, est)


def gateway(valor, forma, recusar):
    """UC10 - simulação do Gateway de Pagamento (ator externo)."""
    if recusar:
        return False, None
    return True, secrets.token_hex(4).upper()


@route("POST", "/api/saida", OP)
def saida(c):
    db, b = c.db, c.body
    est = db.execute("select * from estadias where id=? and status='ativa'", (b.get("estadia_id"),)).fetchone()
    if not est:
        raise ApiError(404, "Registro de entrada não encontrado ou já finalizado.")
    calc = calcular(db, est, agora())
    valor, forma = calc["valor"], b.get("forma")
    if valor > 0 and forma not in FORMAS:
        raise ApiError(400, "Selecione a forma de pagamento.")
    forma = forma if valor > 0 else "Isento"
    ok, aut = (True, "ISENTO") if valor == 0 else gateway(valor, forma, bool(b.get("simular_recusa")))
    db.execute("insert into pagamentos(estadia_id,valor,forma,status,autorizacao,data) values(?,?,?,?,?,?)",
               (est["id"], valor, forma, "aprovado" if ok else "recusado", aut, agora().isoformat()))
    if not ok:
        db.commit()  # mantém o registro da tentativa recusada para auditoria
        raise ApiError(402, "Pagamento recusado. A saída não foi concluída: escolha outra forma de pagamento.")
    saida_dt = agora().isoformat()
    db.execute("update estadias set saida=?, valor=?, forma_pgto=?, status='finalizada' where id=?",
               (saida_dt, valor, forma, est["id"]))
    d = estadia_json(db, est, calc=False)
    d.update(saida=saida_dt, minutos=calc["minutos"], valor=valor, forma=forma, autorizacao=aut,
             detalhe=calc["detalhe"])
    return d


# --- UC05 Registrar avaria / ocorrência
@route("POST", "/api/ocorrencias", OP)
def ocorrencia_nova(c):
    db, b = c.db, c.body
    if b.get("tipo") not in OCORRENCIAS:
        raise ApiError(400, "Selecione o tipo da ocorrência.")
    desc = (b.get("descricao") or "").strip()
    if not desc:
        raise ApiError(400, "Descreva a ocorrência.")
    placa = norm_placa(b.get("placa"))
    est_id, geral = None, True
    if placa:
        est = db.execute("select * from estadias where placa=? and status='ativa'", (placa,)).fetchone()
        if est:
            est_id, geral = est["id"], False
        elif not b.get("confirmar_geral"):
            raise ApiError(409, "Veículo não encontrado no pátio. Registrar como ocorrência geral do pátio?",
                           {"nao_encontrado": True})
    db.execute("insert into ocorrencias(estadia_id,placa,tipo,descricao,data,operador_id) values(?,?,?,?,?,?)",
               (est_id, None if geral else placa, b["tipo"], desc, agora().isoformat(), c.user["id"]))
    return {"ok": True, "geral": geral}


@route("GET", "/api/ocorrencias", OP)
def ocorrencias_lista(c):
    return [dict(r) for r in c.db.execute(
        "select tipo, placa, descricao, data from ocorrencias order by id desc limit 15")]


# --- UC06 Gerenciar mensalistas
def validar_mensalista(db, b, self_id=None):
    nome = (b.get("nome") or "").strip()
    cpf = re.sub(r"\D", "", b.get("cpf") or "")
    placas = []
    for p in re.split(r"[,;\s]+", b.get("placas") or ""):
        p = norm_placa(p)
        if p and p not in placas:
            if not placa_ok(p):
                raise ApiError(400, f"Placa inválida: {p}.")
            placas.append(p)
    if not nome:
        raise ApiError(400, "Informe o nome do cliente.")
    if len(cpf) != 11:
        raise ApiError(400, "CPF deve ter 11 dígitos.")
    if not placas:
        raise ApiError(400, "Informe ao menos uma placa.")
    if b.get("plano") not in PLANOS:
        raise ApiError(400, "Escolha o plano.")
    try:
        date.fromisoformat(b.get("vencimento") or "")
    except ValueError:
        raise ApiError(400, "Informe a data de vencimento.")
    for m in db.execute("select * from mensalistas where id != ?", (self_id or 0,)):
        if m["cpf"] == cpf or set(placas) & {norm_placa(x) for x in m["placas"].split(",")}:
            raise ApiError(409, f"CPF ou placa já cadastrados para {m['nome']}.", {"duplicado": m["id"]})
    return (nome, cpf, (b.get("telefone") or "").strip(), ",".join(placas), b["plano"], b["vencimento"])


@route("GET", "/api/mensalistas", ADM)
def mensalistas_lista(c):
    return [dict(r) for r in c.db.execute("select * from mensalistas order by nome")]


@route("POST", "/api/mensalistas", ADM)
def mensalista_novo(c):
    v = validar_mensalista(c.db, c.body)
    cur = c.db.execute("insert into mensalistas(nome,cpf,telefone,placas,plano,vencimento) values(?,?,?,?,?,?)", v)
    return {"id": cur.lastrowid}


@route("PUT", r"/api/mensalistas/(\d+)", ADM)
def mensalista_edita(c):
    mid = int(c.m.group(1))
    v = validar_mensalista(c.db, c.body, mid)
    c.db.execute("update mensalistas set nome=?,cpf=?,telefone=?,placas=?,plano=?,vencimento=? where id=?", v + (mid,))
    return {"ok": True}


@route("POST", r"/api/mensalistas/(\d+)/status", ADM)
def mensalista_status(c):
    c.db.execute("update mensalistas set ativo=? where id=?", (1 if c.body.get("ativo") else 0, int(c.m.group(1))))
    return {"ok": True}


# --- UC07 Configurar tarifas
@route("GET", "/api/tarifas", ADM)
def tarifas_get(c):
    cfg = {r["chave"]: int(r["valor"]) for r in c.db.execute("select * from config")}
    return {**cfg, "tarifas": {r["tipo"]: dict(r) for r in c.db.execute("select * from tarifas")}}


@route("PUT", "/api/tarifas", ADM)
def tarifas_put(c):
    b, novos = c.body, []

    def num(v, campo, rotulo, inteiro=False, minimo=0):
        try:
            x = float(str(v).replace(",", "."))
        except ValueError:
            raise ApiError(400, f"{rotulo}: informe um número.", {"campo": campo})
        if x < minimo or (inteiro and x != int(x)):
            raise ApiError(400, f"{rotulo}: valor inválido.", {"campo": campo})
        return int(x) if inteiro else x
    tol = num(b.get("tolerancia_min"), "tolerancia_min", "Tolerância", True)
    fr = num(b.get("fracao_min"), "fracao_min", "Fração", True, 1)
    for tipo in TIPOS:
        t = (b.get("tarifas") or {}).get(tipo) or {}
        novos.append((num(t.get("valor_hora"), f"{tipo}.valor_hora", f"{tipo} - 1ª hora"),
                      num(t.get("valor_fracao"), f"{tipo}.valor_fracao", f"{tipo} - fração"),
                      num(t.get("valor_diaria"), f"{tipo}.valor_diaria", f"{tipo} - diária"), tipo))
    c.db.execute("update config set valor=? where chave='tolerancia_min'", (str(tol),))
    c.db.execute("update config set valor=? where chave='fracao_min'", (str(fr),))
    for n in novos:
        c.db.execute("update tarifas set valor_hora=?, valor_fracao=?, valor_diaria=? where tipo=?", n)
    return {"ok": True}


# --- UC08 Relatórios financeiros
def relatorio(db, ini, fim, tipo):
    try:
        i, f = date.fromisoformat(ini), date.fromisoformat(fim)
    except (ValueError, TypeError):
        raise ApiError(400, "Informe a data inicial e a data final.")
    if i > f:
        raise ApiError(400, "A data inicial não pode ser maior que a final.")
    rows = db.execute("select * from estadias where status='finalizada' and date(saida) between ? and ?",
                      (ini, fim)).fetchall()
    grupos = {}
    for e in rows:
        if tipo == "forma":
            k = e["forma_pgto"]
        elif tipo == "diario":
            k = e["saida"][:10]
        else:
            k = "Mensalistas" if e["mensalista_id"] else "Avulsos"
        g = grupos.setdefault(k, [0, 0.0])
        g[0] += 1
        g[1] += e["valor"] or 0
    ordem = sorted(grupos.items(), key=(lambda kv: kv[0]) if tipo == "diario" else (lambda kv: -kv[1][1]))
    rot = {"forma": "Forma de pagamento", "diario": "Dia", "perfil": "Perfil do cliente"}[tipo]
    linhas = [[datetime.fromisoformat(k).strftime("%d/%m/%Y") if tipo == "diario" else k, g[0], round(g[1], 2)]
              for k, g in ordem]
    return {"colunas": [rot, "Saídas", "Receita"], "linhas": linhas,
            "total_qtd": sum(l[1] for l in linhas), "total_valor": round(sum(l[2] for l in linhas), 2)}


@route("GET", "/api/relatorio", ADM)
def relatorio_get(c):
    return relatorio(c.db, c.q.get("ini"), c.q.get("fim"), c.q.get("tipo", "forma"))


@route("GET", "/api/relatorio.csv", ADM)
def relatorio_csv(c):
    r = relatorio(c.db, c.q.get("ini"), c.q.get("fim"), c.q.get("tipo", "forma"))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(r["colunas"])
    for l in r["linhas"]:
        w.writerow([l[0], l[1], f"{l[2]:.2f}".replace(".", ",")])
    w.writerow(["Total", r["total_qtd"], f"{r['total_valor']:.2f}".replace(".", ",")])
    return Resp(buf.getvalue().encode("utf-8-sig"), ctype="text/csv; charset=utf-8",
                headers=[("Content-Disposition", 'attachment; filename="relatorio_parksystem.csv"')])


# --- UC09 Consultar histórico
@route("GET", "/api/usuarios", ADM)
def usuarios(c):
    return [dict(r) for r in c.db.execute("select id, nome from usuarios order by nome")]


@route("GET", "/api/historico", ADM)
def historico(c):
    q, where, args = c.q, [], []
    if q.get("placa"):
        where.append("e.placa like ?")
        args.append("%" + norm_placa(q["placa"]) + "%")
    if q.get("ini"):
        where.append("date(e.entrada) >= ?")
        args.append(q["ini"])
    if q.get("fim"):
        where.append("date(e.entrada) <= ?")
        args.append(q["fim"])
    if q.get("operador"):
        where.append("e.operador_id = ?")
        args.append(q["operador"])
    if q.get("status") in ("ativa", "finalizada"):
        where.append("e.status = ?")
        args.append(q["status"])
    sql = ("select e.*, v.codigo as vaga, u.nome as operador from estadias e "
           "join vagas v on v.id=e.vaga_id join usuarios u on u.id=e.operador_id "
           + ("where " + " and ".join(where) if where else "") + " order by e.entrada desc limit 500")
    rows = c.db.execute(sql, args).fetchall()
    oc = {}
    for o in c.db.execute("select estadia_id, tipo, descricao from ocorrencias where estadia_id is not null"):
        oc.setdefault(o["estadia_id"], []).append(f"{o['tipo']}: {o['descricao']}")
    out = []
    for e in rows:
        fim = datetime.fromisoformat(e["saida"]) if e["saida"] else agora()
        mins = int((fim - datetime.fromisoformat(e["entrada"])).total_seconds() // 60)
        out.append({"ticket": e["ticket"], "placa": e["placa"], "tipo": e["tipo"], "vaga": e["vaga"],
                    "entrada": e["entrada"], "saida": e["saida"], "minutos": mins, "valor": e["valor"],
                    "forma": e["forma_pgto"], "operador": e["operador"], "status": e["status"],
                    "ocorrencias": oc.get(e["id"], [])})
    return out


# ----------------------------------------------------------------- servidor HTTP
class Ctx:
    pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def responder(self, r):
        body = r.data if isinstance(r.data, bytes) else json.dumps(r.data, ensure_ascii=False).encode()
        self.send_response(r.code)
        self.send_header("Content-Type", r.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in r.headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def tratar(self, method):
        u = urlparse(self.path)
        if method == "GET" and u.path in ("/", "/index.html"):
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self.responder(Resp(f.read(), ctype="text/html; charset=utf-8"))
        c = Ctx()
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        c.sid = ck["sid"].value if "sid" in ck else None
        c.user = SESS.get(c.sid)
        c.q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            n = int(self.headers.get("Content-Length") or 0)
            c.body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(c.body, dict):
                c.body = {}
        except ValueError:
            c.body = {}
        for m, rx, perfil, fn in ROUTES:
            c.m = rx.match(u.path)
            if m != method or not c.m:
                continue
            c.db = conectar()
            try:
                if perfil and not c.user:
                    raise ApiError(401, "Sessão expirada. Entre novamente.")
                if perfil in (OP, ADM) and c.user["perfil"] != perfil:
                    raise ApiError(403, "Seu perfil não tem acesso a esta função.")
                r = fn(c)
                c.db.commit()
                return self.responder(r if isinstance(r, Resp) else Resp(r))
            except ApiError as e:
                c.db.rollback()
                return self.responder(Resp({"erro": e.msg, **e.extra}, code=e.code))
            except Exception as e:  # erro inesperado
                c.db.rollback()
                return self.responder(Resp({"erro": f"Erro interno: {e}"}, code=500))
            finally:
                c.db.close()
        self.responder(Resp({"erro": "Não encontrado."}, code=404))

    def do_GET(self):
        self.tratar("GET")

    def do_POST(self):
        self.tratar("POST")

    def do_PUT(self):
        self.tratar("PUT")


if __name__ == "__main__":
    iniciar_banco()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"ParkSystem rodando em {url}  (Ctrl+C para encerrar)")
    if not os.environ.get("PARK_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")
