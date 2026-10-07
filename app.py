import os
import re
import json
import uuid
import time
import requests
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, abort, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from scipy.stats import poisson
from apscheduler.schedulers.background import BackgroundScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv


basedir = os.path.abspath(os.path.dirname(__file__))
caminho_env = os.path.join(basedir, '.env')

load_dotenv(caminho_env, override=True)


from ia_fluxo import processar_analise_chat, gerar_analise_bilhete, selecionar_melhores_jogos_ia


from itsdangerous import URLSafeTimedSerializer
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def jogo_ainda_valido(horario_str):
    """Retorna True se o jogo ainda não começou, False se já passou."""
    try:
        agora_br = datetime.utcnow() - timedelta(hours=3)
        hora, minuto = map(int, horario_str.split(':'))
        hora_jogo = agora_br.replace(hour=hora, minute=minuto, second=0, microsecond=0)
        return agora_br < hora_jogo
    except Exception:
        # Se der erro na leitura do horário, mantém o jogo por segurança
        return True


def cpf_valido(cpf):
    cpf = ''.join(filter(str.isdigit, cpf))

    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    for i in range(9, 11):
        value = sum((int(cpf[num]) * ((i + 1) - num) for num in range(0, i)))
        digit = ((value * 10) % 11) % 10
        if digit != int(cpf[i]):
            return False
    return True


try:
    from config_ligas import LIGAS_COMPLETAS
except ImportError:
    LIGAS_COMPLETAS = {}

app = Flask(__name__)

# confi de segurança e api (via env)

def obrigatoria(nome_var):
    valor = os.environ.get(nome_var)
    if not valor:
        raise RuntimeError(
            f"Variável de ambiente obrigatória '{nome_var}' não configurada. "
            f"Defina-a no arquivo .env antes de iniciar o servidor."
        )
    return valor


app.secret_key = obrigatoria('FLUXO_SECRET_KEY')
s = URLSafeTimedSerializer(app.secret_key)

ADMIN_MASTER_KEY = obrigatoria('FLUXO_ADMIN_KEY')
WEBHOOK_SECRET_TOKEN = obrigatoria('WEBHOOK_TOKEN')

ODDS_API_KEY_1 = os.environ.get('ODDS_API_KEY_1', '')
ODDS_API_KEY_2 = os.environ.get('ODDS_API_KEY_2', '')
ASAAS_API_KEY = os.environ.get('ASAAS_API_KEY', '')
URL_ASAAS = "https://api.asaas.com/v3"  # URL de Produção

EMAIL_USER = os.environ.get('EMAIL_USER', '')
EMAIL_PASS = os.environ.get('EMAIL_PASS', '')

if not ODDS_API_KEY_1:
    print("AVISO: ODDS_API_KEY_1 não configurada — o site vai rodar em modo simulado/sem jogos reais.")

# CONFIGURAÇÃO DO RATE LIMIT
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "200 per hour"],
    storage_uri="memory://"  # Salva na memória RAM. Se usar múltiplos servidores, mude para Redis.
)

# --- BANCO DE DADOS ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'fluxobet.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


# ==========================================
#modelos
# ==========================================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    senha = db.Column(db.String(200), nullable=False)
    cpf = db.Column(db.String(20), unique=True, nullable=False)
    telefone = db.Column(db.String(20), nullable=False)
    nascimento = db.Column(db.String(20))
    vip = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    data_cadastro = db.Column(db.DateTime, default=datetime.utcnow)

    # --- Segurança de login ---
    tentativas_falhas = db.Column(db.Integer, default=0)
    bloqueado_ate = db.Column(db.DateTime, nullable=True)

    # --- Sistema de afiliados ---
    indicado_por = db.Column(db.Integer, nullable=True)
    saldo_comissao = db.Column(db.Float, default=0.0)


class DeviceLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sistema = db.Column(db.String(50))
    navegador = db.Column(db.String(50))
    ip = db.Column(db.String(50))
    cidade = db.Column(db.String(100))
    estado = db.Column(db.String(50))
    data_login = db.Column(db.DateTime, default=datetime.utcnow)


class ChatUsage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    data_uso = db.Column(db.Date, default=datetime.utcnow().date)
    contagem = db.Column(db.Integer, default=0)


class TicketUsage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    data_uso = db.Column(db.Date, default=datetime.utcnow().date)
    contagem = db.Column(db.Integer, default=0)


class GameInsight(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    id_api_jogo = db.Column(db.String(50), unique=True, nullable=False)
    data_geracao = db.Column(db.Date, default=datetime.utcnow().date)
    analise_texto = db.Column(db.Text, nullable=False)
    jogador_mercado = db.Column(db.String(100))
    jogador_odd = db.Column(db.Float)
    cantos_mercado = db.Column(db.String(100))
    cantos_odd = db.Column(db.Float)


with app.app_context():
    db.create_all()


PLANOS = {
    'mensal': {'nome': 'Plano Mensal Fluxobet', 'valor': 2500},        # R$ 25,00
    'semestral': {'nome': 'Plano Semestral Fluxobet', 'valor': 11990},  # R$ 119,90
    'anual': {'nome': 'Plano Anual Fluxobet', 'valor': 19990}          # R$ 199,90
}

DADOS_CACHE = []

CACHE_BILHETES = {
    'baixo': {'dados': None, 'tempo': 0},
    'medio': {'dados': None, 'tempo': 0},
    'alto': {'dados': None, 'tempo': 0},
    'zebra': {'dados': None, 'tempo': 0}
}
TEMPO_CACHE_HORAS = 8

# ==========================================
# logica de apostas e jogos
# ==========================================


def calcular_poisson(media_casa, media_fora):
    prob_casa = prob_empate = prob_fora = 0
    for i in range(6):
        for j in range(6):
            prob = poisson.pmf(i, media_casa) * poisson.pmf(j, media_fora)
            if i > j:
                prob_casa += prob
            elif i == j:
                prob_empate += prob
            else:
                prob_fora += prob
    return prob_casa, prob_empate, prob_fora


MAPA_CHAVES_API = {
    'soccer_epl': 'Premier League',
    'soccer_uefa_champs_league': 'Champions League',
    'soccer_spain_la_liga': 'La Liga',
    'soccer_brazil_campeonato': 'Brasileirão Série A',
    'soccer_england_championship': 'Championship',
    'soccer_italy_serie_a': 'Serie A (ITA)',
    'soccer_france_ligue_one': 'Ligue 1',
    'soccer_germany_bundesliga': 'Bundesliga',
    'soccer_conmebol_libertadores': 'Libertadores'
}

MODO_DESENVOLVEDOR = False


def gerar_jogos_simulados():
    print("Modo DEV ativo: retornando lista vazia para forçar a tela de 'Sem Jogos' e economizar API...")
    return []


# ==========================================
# sistema de cache (p economizar requisições)
# ==========================================
CACHE_JOGOS = {
    "dados": [],
    "ultima_atualizacao": None
}
TEMPO_CACHE_MINUTOS = 120  # O cache dura 2 horas


def buscar_jogos_do_dia():
    global CACHE_JOGOS, ODDS_API_KEY_1, ODDS_API_KEY_2

    if MODO_DESENVOLVEDOR:
        return gerar_jogos_simulados()

    fuso_br = timezone(timedelta(hours=-3))
    agora_br = datetime.now(timezone.utc).astimezone(fuso_br)
    hoje_data = agora_br.date()

    print(f"Data BR para busca: {hoje_data} | Hora: {agora_br.strftime('%H:%M')}")

    # Verifica o cache antes de gastar API
    if CACHE_JOGOS.get("dados") and CACHE_JOGOS.get("ultima_atualizacao"):
        tempo_passado = agora_br - CACHE_JOGOS["ultima_atualizacao"]
        if (tempo_passado < timedelta(minutes=TEMPO_CACHE_MINUTOS)
                and CACHE_JOGOS["ultima_atualizacao"].date() == hoje_data):
            print("Retornando jogos do CACHE (economizando requisições da API).")
            return CACHE_JOGOS["dados"]

    if not ODDS_API_KEY_1:
        return gerar_jogos_simulados()

    ligas_principais = [
        'soccer_epl', 'soccer_brazil_campeonato', 'soccer_france_ligue_one',
        'soccer_spain_la_liga', 'soccer_italy_serie_a', 'soccer_conmebol_copa_libertadores',
        'soccer_conmebol_copa_sudamericana', 'soccer_uefa_champs_league',
        'soccer_uefa_champs_league_qualification',
    ]

    ligas_secundarias = [
        'soccer_uefa_champs_league', 'soccer_uefa_europa_league', 'soccer_england_fa_cup',
        'soccer_spain_copa_del_rey', 'soccer_efl_championship',
    ]

    jogos_processados = []
    api_funcionou = False
    META_JOGOS = 15

    def realizar_chamada(lista_ligas):
        nonlocal api_funcionou
        for liga_key in lista_ligas:
            if len(jogos_processados) >= META_JOGOS:
                print(f"Meta de {META_JOGOS} jogos atingida! Abortando buscas para economizar créditos.")
                break

            if 'portugal' in liga_key:
                continue

            # Respira para não estourar o limite de velocidade da API
            time.sleep(1.5)

            url = f"https://api.the-odds-api.com/v4/sports/{liga_key}/odds/?apiKey={ODDS_API_KEY_1}&regions=eu&markets=h2h"

            try:
                response = requests.get(url, timeout=10)

                # Failover entre duas chaves de API
                if response.status_code in [401, 429] and ODDS_API_KEY_2:
                    print(f"API 1 falhou ({response.status_code}), alternando para API 2 na liga {liga_key}...")
                    url_fallback = url.replace(ODDS_API_KEY_1, ODDS_API_KEY_2)
                    time.sleep(1.5)
                    response = requests.get(url_fallback, timeout=10)

                if response.status_code != 200:
                    continue

                dados_api = response.json()

                if isinstance(dados_api, dict) and dados_api.get('message'):
                    print(f"API recusou (liga {liga_key}): {dados_api}")
                    continue

                api_funcionou = True

                for match in dados_api:
                    try:
                        time_utc = datetime.strptime(
                            match['commence_time'][:19], "%Y-%m-%dT%H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        time_br = time_utc.astimezone(fuso_br)
                    except Exception as e:
                        print(f"Erro ao ler horário do jogo: {e}")
                        continue

                    casa = match.get('home_team', 'Time Casa')
                    fora = match.get('away_team', 'Time Fora')

                    if time_br.date() != hoje_data:
                        continue

                    liga_nome_exato = MAPA_CHAVES_API.get(liga_key, match.get('sport_title', 'Futebol'))
                    liga_logo_real = "https://cdn-icons-png.flaticon.com/512/5328/5328082.png"
                    liga_categoria_real = "FUTEBOL"

                    if LIGAS_COMPLETAS:
                        for categoria, ligas in LIGAS_COMPLETAS.items():
                            for l in ligas:
                                if l.get('nome') and l.get('nome').lower() in liga_nome_exato.lower():
                                    liga_logo_real = f"https://media.api-sports.io/football/leagues/{l.get('id')}.png"
                                    liga_categoria_real = categoria
                                    break

                    odd_c, odd_e, odd_f = 2.0, 3.0, 3.5
                    if match.get('bookmakers'):
                        try:
                            m = match['bookmakers'][0].get('markets', [{}])[0].get('outcomes', [])
                            for o in m:
                                if o['name'] == casa:
                                    odd_c = o['price']
                                elif o['name'] == fora:
                                    odd_f = o['price']
                                elif o['name'] in ('Draw', 'Empate'):
                                    odd_e = o['price']
                        except Exception:
                            pass

                    jogos_processados.append({
                        "id_api": str(match.get('id', '0')),
                        "liga_nome": liga_nome_exato,
                        "liga_logo": liga_logo_real,
                        "liga_categoria": liga_categoria_real,
                        "partida": f"{casa} x {fora}",
                        "times": {"casa": casa, "fora": fora},
                        "horario": time_br.strftime("%H:%M"),
                        "status": "NS",
                        "dica_odd": odd_c if odd_c < odd_f else odd_f,
                        "dica_confianca": int((1 / min(odd_c, odd_f)) * 100) if min(odd_c, odd_f) > 0 else 50,
                        "probs": {"casa": int((1 / odd_c) * 100), "empate": int((1 / odd_e) * 100), "fora": int((1 / odd_f) * 100)},
                        "stats": {"casa": {"book": odd_c}, "empate": {"book": odd_e}, "fora": {"book": odd_f}},
                        "liga_slug": str(liga_nome_exato).lower().replace(" ", "-").replace("é", "e").replace("ã", "a"),
                        "forma": {"casa": ['V', 'E', 'D', 'V', 'V'], "fora": ['D', 'E', 'D', 'D', 'V']},
                        "medias": {"casa": 2.0, "fora": 1.2},
                        "specials": {
                            "jogador": "Destaque: +1.5 Chutes", "jogador_odd": 1.85, "jogador_prob": 80,
                            "cantos": "+8.5 Cantos", "cantos_odd": 1.55
                        }
                    })
            except Exception as e:
                print(f"Erro na liga {liga_key}: {e}")

    realizar_chamada(ligas_principais)
    if len(jogos_processados) < META_JOGOS:
        realizar_chamada(ligas_secundarias)

    if api_funcionou:
        CACHE_JOGOS["dados"] = jogos_processados
        CACHE_JOGOS["ultima_atualizacao"] = agora_br
        return jogos_processados
    else:
        return gerar_jogos_simulados()


def scanner():
    global DADOS_CACHE
    novos_dados = buscar_jogos_do_dia()
    if novos_dados:
        DADOS_CACHE = novos_dados
        try:
            caminho = os.path.join(basedir, 'jogos_cache.json')
            with open(caminho, 'w', encoding='utf-8') as f:
                json.dump(novos_dados, f, ensure_ascii=False)
        except Exception as e:
            print(f"Erro ao salvar jogos_cache.json: {e}")


scheduler = BackgroundScheduler()
scheduler.add_job(scanner, 'interval', minutes=480)
scheduler.add_job(scanner, 'date', run_date=datetime.now(timezone.utc) + timedelta(seconds=5))
scheduler.start()


def gerar_menu():
    menu = {}
    if not DADOS_CACHE:
        return menu

    try:
        if LIGAS_COMPLETAS:
            ordem = list(LIGAS_COMPLETAS.keys())
        else:
            ordem = list(set([j.get('liga_categoria', 'FUTEBOL') for j in DADOS_CACHE]))
    except Exception:
        ordem = ["FUTEBOL"]

    for cat in ordem:
        menu[cat] = []

    ligas_vistas = set()
    for jogo in DADOS_CACHE:
        categoria = jogo.get('liga_categoria', 'FUTEBOL')
        nome_liga = jogo.get('liga_nome', 'Outros')
        slug_liga = jogo.get('liga_slug', 'outros')

        chave = f"{categoria}-{nome_liga}"

        if chave not in ligas_vistas:
            if categoria not in menu:
                menu[categoria] = []

            qtd = len([j for j in DADOS_CACHE if j.get('liga_nome') == nome_liga])

            menu[categoria].append({
                "nome": nome_liga,
                "slug": slug_liga,
                "logo": jogo.get('liga_logo', ""),
                "qtd": qtd
            })
            ligas_vistas.add(chave)
    return menu


def enviar_email_recuperacao(destinatario, link_recuperacao):
    if not EMAIL_USER or not EMAIL_PASS:
        print("Aviso: EMAIL_USER/EMAIL_PASS não configurados — e-mail não enviado.")
        return

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = destinatario
    msg['Subject'] = "Recuperação de Senha - FluxoBet"
    corpo = (
        "Olá,\n\nRecebemos uma solicitação para redefinir a senha da sua conta.\n"
        f"Para cadastrar uma nova senha, clique no link abaixo:\n{link_recuperacao}\n\n"
        "Atenciosamente,\nSuporte FluxoBet"
    )
    msg.attach(MIMEText(corpo, 'plain'))
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, destinatario, msg.as_string())
        server.quit()
    except Exception as e:
        print(f"Erro ao enviar e-mail: {e}")


# ==========================================
# rotas publicas e auntenticação
# ==========================================

@app.before_request
def verificar_acesso():
    rotas_abertas = [
        'index', 'login', 'cadastro', 'static', 'termos', 'jogo_responsavel',
        'webhook_asaas', 'recuperar_senha', 'redefinir_senha', 'analisar_aposta', 'gerar_bilhete'
    ]
    if request.endpoint and request.endpoint not in rotas_abertas and 'user_id' not in session and 'static' not in request.endpoint:
        if not request.path.startswith('/api') and not request.path.startswith('/webhook'):
            return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == 'POST':
        identificador = request.form.get('usuario')
        senha_login = request.form.get('senha')
        user = User.query.filter((User.email == identificador) | (User.cpf == identificador)).first()

        if user:
            if user.bloqueado_ate and user.bloqueado_ate > datetime.utcnow():
                tempo_restante = (user.bloqueado_ate - datetime.utcnow()).seconds // 60
                return render_template(
                    'login.html',
                    erro=f"Conta bloqueada por excesso de tentativas. Tente novamente em {tempo_restante + 1} minutos."
                )

            if check_password_hash(user.senha, senha_login):
                user.tentativas_falhas = 0
                user.bloqueado_ate = None
                db.session.commit()

                session['user_id'] = user.id
                session['user_nome'] = user.nome
                session['user_email'] = user.email
                session['plano_ativo'] = user.vip

                ip_usuario = request.headers.get('X-Forwarded-For', request.remote_addr)
                sis = request.user_agent.platform or "Desconhecido"
                nav = request.user_agent.browser or "Desconhecido"
                cidade, estado = "Desconhecido", "BR"

                if ip_usuario and ip_usuario != '127.0.0.1':
                    try:
                        loc_data = requests.get(f"http://ip-api.com/json/{ip_usuario}", timeout=2).json()
                        if loc_data.get("status") == "success":
                            cidade = loc_data.get("city")
                            estado = loc_data.get("region")
                    except Exception:
                        pass
                else:
                    cidade, estado = "Servidor Local", "DEV"

                novo_dispositivo = DeviceLog(
                    user_id=user.id, sistema=str(sis).capitalize(),
                    navegador=str(nav).capitalize(), ip=ip_usuario,
                    cidade=cidade, estado=estado
                )
                db.session.add(novo_dispositivo)
                db.session.commit()

                return redirect(url_for('index'))

            else:
                user.tentativas_falhas += 1
                if user.tentativas_falhas >= 5:
                    user.bloqueado_ate = datetime.utcnow() + timedelta(minutes=15)
                    db.session.commit()
                    return render_template(
                        'login.html',
                        erro="Muitas tentativas falhas. Conta bloqueada por 15 minutos por segurança."
                    )

                db.session.commit()
                return render_template(
                    'login.html',
                    erro=f"Senha incorreta. Tentativa {user.tentativas_falhas} de 5."
                )

        return render_template('login.html', erro="Dados incorretos.")
    return render_template('login.html')


@app.route('/cadastro', methods=['GET', 'POST'])
@limiter.limit("5 per hour", methods=["POST"])
def cadastro():
    ref_id = request.args.get('ref')
    if ref_id and request.method == 'GET':
        session['indicado_por'] = ref_id

    if request.method == 'POST':
        # Honeypot anti-bot: campo invisível que só um bot preencheria
        honeypot = request.form.get('website_url')
        if honeypot:
            return redirect(url_for('login'))

        nome = request.form.get('nome')
        email = request.form.get('email', '').strip().lower()
        cpf = request.form.get('cpf')
        telefone = request.form.get('telefone', '')
        nascimento = request.form.get('nascimento')
        senha = request.form.get('senha')

        padrao_email = r'^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$'
        if not re.match(padrao_email, email):
            return render_template('cadastro.html', erro="E-mail inválido! Use um formato real (ex: nome@gmail.com).")

        tel_limpo = ''.join(filter(str.isdigit, telefone))
        if len(tel_limpo) < 10 or len(tel_limpo) > 11:
            return render_template('cadastro.html', erro="Telefone inválido! Digite o DDD e o número corretamente.")

        if not cpf_valido(cpf):
            return render_template('cadastro.html', erro="CPF inválido! Por favor, confira os números.")

        if User.query.filter_by(email=email).first():
            return render_template('cadastro.html', erro="E-mail já cadastrado.")
        if User.query.filter_by(cpf=cpf).first():
            return render_template('cadastro.html', erro="CPF já cadastrado.")

        afiliado_id = session.get('indicado_por')

        novo_user = User(
            nome=nome,
            email=email,
            cpf=cpf,
            telefone=tel_limpo,
            nascimento=nascimento,
            senha=generate_password_hash(senha),
            vip=False,
            indicado_por=afiliado_id
        )

        try:
            db.session.add(novo_user)
            db.session.commit()
            session.pop('indicado_por', None)
            return redirect(url_for('login', sucesso="Conta criada com sucesso! Faça login."))
        except Exception:
            db.session.rollback()
            return render_template('cadastro.html', erro="Erro interno ao salvar. Tente novamente.")

    return render_template('cadastro.html')


@app.route('/recuperar-senha', methods=['GET', 'POST'])
def recuperar_senha():
    if request.method == 'POST':
        identificador = request.form.get('usuario')
        user = User.query.filter((User.email == identificador) | (User.cpf == identificador)).first()
        if user:
            token = s.dumps(user.email, salt='recuperacao-senha')
            link = url_for('redefinir_senha', token=token, _external=True)
            enviar_email_recuperacao(user.email, link)
        return render_template('recuperar_senha.html', sucesso="Enviamos o link para o seu e-mail.")
    return render_template('recuperar_senha.html')


@app.route('/redefinir-senha/<token>', methods=['GET', 'POST'])
def redefinir_senha(token):
    try:
        email = s.loads(token, salt='recuperacao-senha', max_age=3600)
    except Exception:
        return render_template('redefinir_senha.html', erro="Link inválido ou expirado.")

    if request.method == 'POST':
        user = User.query.filter_by(email=email).first()
        if user:
            user.senha = generate_password_hash(request.form.get('senha_nova'))
            db.session.commit()
            return redirect(url_for('login', sucesso="Senha alterada!"))
    return render_template('redefinir_senha.html')


@app.route('/')
def index():
    global DADOS_CACHE
    if not DADOS_CACHE:
        caminho_cache = os.path.join(basedir, 'jogos_cache.json')
        if os.path.exists(caminho_cache):
            try:
                with open(caminho_cache, 'r', encoding='utf-8') as f:
                    DADOS_CACHE = json.load(f)
            except Exception:
                pass

    if 'user_id' not in session:
        return render_template('landing.html')

    return render_template(
        'index.html',
        menu=gerar_menu(),
        jogos_destaque=DADOS_CACHE[:5],
        user_nome=session.get('user_nome')
    )


@app.route('/liga/<slug>')
def liga(slug):
    jogos = [j for j in DADOS_CACHE if j['liga_slug'] == slug]
    nome = jogos[0]['liga_nome'] if jogos else "Liga"
    logo = jogos[0]['liga_logo'] if jogos else ""
    return render_template('league.html', jogos=jogos, nome_liga=nome, logo_liga=logo, menu=gerar_menu())


@app.route('/partida/<match_id>')
def detalhes_partida(match_id):
    jogo = next((j for j in DADOS_CACHE if str(j['id_api']) == str(match_id)), None)

    if jogo:
        insight_salvo = GameInsight.query.filter_by(id_api_jogo=str(match_id)).first()

        if insight_salvo:
            jogo['analise_ia'] = insight_salvo.analise_texto
            jogo['specials']['jogador'] = insight_salvo.jogador_mercado
            jogo['specials']['jogador_odd'] = insight_salvo.jogador_odd
            jogo['specials']['cantos'] = insight_salvo.cantos_mercado
            jogo['specials']['cantos_odd'] = insight_salvo.cantos_odd
        else:
            from ia_fluxo import gerar_insights_detalhados
            novos_insights = gerar_insights_detalhados(jogo)

            novo_cache = GameInsight(
                id_api_jogo=str(match_id),
                analise_texto=novos_insights.get('analise_texto', 'Análise baseada em volume de mercado e momento das equipes.'),
                jogador_mercado=novos_insights.get('jogador_mercado', 'Mais de 1.5 finalizações'),
                jogador_odd=float(novos_insights.get('jogador_odd', 1.80)),
                cantos_mercado=novos_insights.get('cantos_mercado', 'Mais de 8.5 cantos'),
                cantos_odd=float(novos_insights.get('cantos_odd', 1.60))
            )
            db.session.add(novo_cache)
            db.session.commit()

            jogo['analise_ia'] = novo_cache.analise_texto
            jogo['specials']['jogador'] = novo_cache.jogador_mercado
            jogo['specials']['jogador_odd'] = novo_cache.jogador_odd
            jogo['specials']['cantos'] = novo_cache.cantos_mercado
            jogo['specials']['cantos_odd'] = novo_cache.cantos_odd

        return render_template('match_detail.html', jogo=jogo, menu=gerar_menu())

    return redirect(url_for('index'))


@app.route('/termos')
def termos():
    return render_template('termos.html')


@app.route('/planos')
def planos():
    return render_template('planos.html')


@app.route('/jogo-responsavel')
def jogo_responsavel():
    return render_template('jogo_responsavel.html')


@app.route('/sair')
def sair():
    session.clear()
    return redirect(url_for('login'))


@app.route('/perfil')
def perfil():
    return render_template('profile.html', user=User.query.get(session.get('user_id')), menu=gerar_menu())


@app.route('/gerenciar-dispositivos')
def gerenciar_dispositivos():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    dispositivos_bd = DeviceLog.query.filter_by(user_id=session['user_id']).order_by(DeviceLog.data_login.desc()).limit(5).all()
    return render_template('gerenciar_dispositivos.html', user=User.query.get(session['user_id']), dispositivos=dispositivos_bd)


@app.route('/alterar-senha', methods=['GET', 'POST'])
def alterar_senha():
    if request.method == 'POST':
        senha_atual, nova_senha = request.form.get('senha_atual'), request.form.get('senha_nova')
        user = User.query.get(session['user_id'])
        if not check_password_hash(user.senha, senha_atual):
            return render_template('alterar_senha.html', erro="A senha atual está incorreta.")
        user.senha = generate_password_hash(nova_senha)
        db.session.commit()
        return render_template('alterar_senha.html', sucesso="Senha atualizada com sucesso!")
    return render_template('alterar_senha.html')


# ==========================================
# rotas da ia (chat e bilhetes)
# ==========================================

@app.route('/api/analisar-aposta', methods=['POST'])
def analisar_aposta():
    dados = request.get_json()
    if not dados or not dados.get('aposta'):
        return jsonify({"erro": "Mensagem vazia."}), 400

    aposta_usuario = str(dados.get('aposta')).strip()
    is_vip = session.get('plano_ativo')

    if len(aposta_usuario) < 5:
        return jsonify({
            "status": "success",
            "mensagem_html": "<div class='alert' style='background: rgba(255,255,255,0.05); color: #ccc; border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; padding: 15px;'><strong>Mensagem muito curta!</strong><br>Por favor, digite o nome dos times ou o jogo que você deseja analisar (exemplo: Flamengo x Vasco).</div>"
        })

    limite_diario = 50 if is_vip else 2

    hoje = datetime.utcnow().date()
    uso_chat = ChatUsage.query.filter_by(user_id=session['user_id'], data_uso=hoje).first()

    if not uso_chat:
        uso_chat = ChatUsage(user_id=session['user_id'], data_uso=hoje, contagem=0)
        db.session.add(uso_chat)
        db.session.commit()

    if uso_chat.contagem >= limite_diario:
        if not is_vip:
            msg = "<strong>Limite Gratuito Atingido!</strong><br>Você já usou suas 2 análises diárias gratuitas. <a href='/planos' style='color: #ff4d4d; text-decoration: underline; font-weight: bold;'>Desbloqueie o VIP</a> para ter 50 consultas ao Robô e sinais exclusivos!"
        else:
            msg = "<strong>Limite VIP Atingido!</strong><br>Você atingiu o teto de 50 análises diárias. Esse limite existe para garantir a velocidade do sistema para todos. Volte amanhã para novas análises!"

        return jsonify({
            "status": "success",
            "mensagem_html": f"<div class='alert' style='background: rgba(255, 77, 77, 0.1); color: #ff4d4d; border: 1px solid #ff4d4d; border-radius: 12px; padding: 15px;'>{msg}</div>"
        })

    try:
        resultado_html = processar_analise_chat(aposta_usuario)

        if "429" in resultado_html or "RESOURCE_EXHAUSTED" in resultado_html:
            return jsonify({
                "status": "success",
                "mensagem_html": "<div class='alert' style='background: rgba(255,193,7,0.1); color: #ffc107; border: 1px solid #ffc107; border-radius: 12px;'><strong>Sistema muito requisitado!</strong><br>Muitos clientes estão consultando a base de dados agora. Por favor, aguarde 30 segundos e envie novamente.</div>"
            })

        uso_chat.contagem += 1
        db.session.commit()

        return jsonify({"status": "success", "mensagem_html": resultado_html})
    except Exception:
        return jsonify({"status": "error", "mensagem_html": "Erro de conexão com o sistema."})


@app.route('/api/gerar-bilhete', methods=['POST'])
def gerar_bilhete():
    global DADOS_CACHE
    if not DADOS_CACHE:
        caminho_cache_jogos = os.path.join(basedir, 'jogos_cache.json')
        if os.path.exists(caminho_cache_jogos):
            try:
                with open(caminho_cache_jogos, 'r', encoding='utf-8') as f:
                    DADOS_CACHE = json.load(f)
            except Exception as e:
                print(f"Erro ao ler jogos_cache.json: {e}")

    is_vip = session.get('plano_ativo')
    perfil = request.json.get('risco', 'baixo')

    if not is_vip and perfil != 'baixo':
        return jsonify({"status": "error", "mensagem": f"O perfil {perfil.upper()} é exclusivo VIP..."}), 403

    hoje = datetime.utcnow().date()
    uso_ticket = None

    if not is_vip:
        uso_ticket = TicketUsage.query.filter_by(user_id=session['user_id'], data_uso=hoje).first()
        if not uso_ticket:
            uso_ticket = TicketUsage(user_id=session['user_id'], data_uso=hoje, contagem=0)
            db.session.add(uso_ticket)
            db.session.commit()

        if uso_ticket.contagem >= 1:
            return jsonify({"status": "error", "mensagem": "Você já gerou seu bilhete gratuito de hoje!"}), 403

    tempo_atual = time.time()

    caminho_cache_perfil = os.path.join(basedir, f'cache_bilhete_{perfil}.json')
    cache = {}

    if os.path.exists(caminho_cache_perfil):
        try:
            with open(caminho_cache_perfil, 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except json.JSONDecodeError:
            pass

    if cache and cache.get('jogos_brutos') and (tempo_atual - cache.get('tempo', 0)) < (TEMPO_CACHE_HORAS * 3600):
        jogos_validos_cache = []
        odd_recalculada = 1.0

        for item in cache['jogos_brutos']:
            nome_ia = str(item.get('times', '')).strip().lower().replace(' vs ', ' x ').replace(' - ', ' x ')
            jogo_ref = None

            for j in DADOS_CACHE:
                partida_cache = j['partida'].lower()
                if nome_ia in partida_cache or partida_cache in nome_ia:
                    jogo_ref = j
                    break

                if ' x ' in nome_ia:
                    times_ia = nome_ia.split(' x ')
                    if len(times_ia) == 2:
                        palavra_casa = times_ia[0].strip().split()[0] if times_ia[0].strip() else ""
                        palavra_fora = times_ia[1].strip().split()[0] if times_ia[1].strip() else ""
                        if palavra_casa and palavra_fora and palavra_casa in partida_cache and palavra_fora in partida_cache:
                            jogo_ref = j
                            break

            if jogo_ref and jogo_ainda_valido(jogo_ref['horario']):
                jogos_validos_cache.append(item)
                try:
                    ov = float(str(item.get('odd', '1.0')).replace(',', '.').strip())
                    if ov > 1.0:
                        odd_recalculada *= ov
                except Exception:
                    pass

        if len(jogos_validos_cache) > 0:
            odd_recalculada = round(odd_recalculada, 2)
            prob_ex = {'baixo': 85, 'medio': 65, 'alto': 40, 'zebra': 15}.get(perfil, 50)
            data_g = datetime.now().strftime("%d/%m/%Y as %H:%M")

            texto_z = f"FLUXOBET - BILHETE ROBÔ FLUXOBET ({perfil.upper()})\nData: {data_g}\n\n"
            for it in jogos_validos_cache:
                texto_z += f"Jogo: {it['times']}\nMercado: {it.get('mercado', 'Estatísticas')}\nPalpite: {it['palpite']}\nOdd: {it.get('odd', '1.0')}\n\n"
            texto_z += f"Odd Total: {odd_recalculada}\nAssine: www.fluxobet.com"

            if not is_vip and uso_ticket:
                uso_ticket.contagem += 1
                db.session.commit()

            return jsonify({
                "status": "success",
                "bilhete_html": render_template('components/bilhete_resultado.html', items=jogos_validos_cache, total=odd_recalculada, data_geracao=data_g, total_prob=prob_ex),
                "texto_copiar": texto_z
            })

    jogos = [j for j in DADOS_CACHE if j['status'] != 'FT' and jogo_ainda_valido(j['horario'])]
    jogos_para_ia = sorted(jogos, key=lambda x: x.get('dica_confianca', 0), reverse=True)[:25]

    bilhete_selecionado = selecionar_melhores_jogos_ia(jogos_para_ia, perfil)

    if not bilhete_selecionado or 'bilhete' not in bilhete_selecionado:
        return jsonify({"status": "error", "mensagem": "O sistema não encontrou jogos de valor no momento."})

    odd_multiplicada = 1.0
    for item in bilhete_selecionado['bilhete']:
        try:
            odd_v = float(str(item.get('odd', '1.0')).replace(',', '.').strip())
            if odd_v > 1.0:
                odd_multiplicada *= odd_v
        except Exception:
            pass

    odd_multiplicada = round(odd_multiplicada, 2)
    if odd_multiplicada <= 1.0:
        odd_multiplicada = 2.00

    prob_exibicao = {'baixo': 85, 'medio': 65, 'alto': 40, 'zebra': 15}.get(perfil, 50)
    data_geracao = datetime.now().strftime("%d/%m/%Y as %H:%M")

    texto_zap = f"FLUXOBET - BILHETE IA ({perfil.upper()})\nData: {data_geracao}\n\n"
    for item in bilhete_selecionado['bilhete']:
        mercado = item.get('mercado', 'Estatísticas')
        texto_zap += f"Jogo: {item['times']}\nMercado: {mercado}\nPalpite: {item['palpite']}\nOdd: {item.get('odd', '1.0')}\n\n"
    texto_zap += f"Odd Total: {odd_multiplicada}\nAssine: www.fluxobet.com"

    resposta = {
        "status": "success",
        "bilhete_html": render_template('components/bilhete_resultado.html', items=bilhete_selecionado['bilhete'], total=odd_multiplicada, data_geracao=data_geracao, total_prob=prob_exibicao),
        "texto_copiar": texto_zap
    }

    dados_salvar = {
        'dados': resposta,
        'tempo': tempo_atual,
        'jogos_brutos': bilhete_selecionado['bilhete']
    }

    try:
        with open(caminho_cache_perfil, 'w', encoding='utf-8') as f:
            json.dump(dados_salvar, f, ensure_ascii=False)
    except Exception as e:
        print(f"Erro ao salvar arquivo cache do bilhete: {e}")

    CACHE_BILHETES[perfil] = dados_salvar

    if not is_vip and uso_ticket:
        uso_ticket.contagem += 1
        db.session.commit()

    return jsonify(resposta)


@app.route('/api/odd-de-ouro', methods=['POST'])
def odd_de_ouro():
    global DADOS_CACHE
    if not DADOS_CACHE:
        caminho_cache_jogos = os.path.join(basedir, 'jogos_cache.json')
        if os.path.exists(caminho_cache_jogos):
            try:
                with open(caminho_cache_jogos, 'r', encoding='utf-8') as f:
                    DADOS_CACHE = json.load(f)
            except Exception as e:
                print(f"Erro ao ler jogos_cache.json: {e}")

    is_vip = session.get('plano_ativo')

    if not is_vip:
        return jsonify({
            "status": "error",
            "mensagem": "A Odd de Ouro é um recurso exclusivo VIP! Faça o upgrade para receber o bilhete mais seguro do dia."
        }), 403

    tempo_atual = time.time()

    caminho_cache_ouro = os.path.join(basedir, 'cache_ouro.json')
    cache_ouro = {}

    if os.path.exists(caminho_cache_ouro):
        try:
            with open(caminho_cache_ouro, 'r', encoding='utf-8') as f:
                cache_ouro = json.load(f)
        except json.JSONDecodeError:
            pass

    if cache_ouro and cache_ouro.get('dados') and (tempo_atual - cache_ouro.get('tempo', 0)) < (TEMPO_CACHE_HORAS * 3600):
        return jsonify(cache_ouro['dados'])

    jogos = [j for j in DADOS_CACHE if j['status'] != 'FT' and jogo_ainda_valido(j['horario'])]

    if not jogos:
        return jsonify({"status": "error", "mensagem": "Não há jogos suficientes no radar agora."})

    lista_jogos_com_stats = ""
    for j in jogos[:30]:
        casa = j.get('times', {}).get('casa', 'Casa')
        fora = j.get('times', {}).get('fora', 'Fora')
        prob = j.get('probs', {'casa': 33, 'empate': 33, 'fora': 33})
        med = j.get('medias', {'casa': 1.5, 'fora': 1.2})
        lista_jogos_com_stats += f"- {casa} x {fora} | Probs: Casa {prob['casa']}% - Fora {prob['fora']}% | Médias Gols: {med['casa']} (Casa) - {med['fora']} (Fora)\n"

    num_jogos = len(jogos)

    if num_jogos < 6:
        prompt_odd_ouro = f"""Aja como um analista esportivo de elite. TEMOS POUCOS JOGOS, ENTÃO ATIVE O MODO MISTO (BET BUILDER).
        Analise os jogos REAIS abaixo e escolha EXATAMENTE 6 entradas para um bilhete combinado:
        {lista_jogos_com_stats}

        REGRAS DO MODO MISTO:
        1. REPETIR JOGOS: Você DEVE usar o mesmo jogo 2 ou 3 vezes, mas em mercados diferentes.
        2. VARIAR MERCADOS: Para cada jogo, escolha entre: Vitória/Empate, Total de Gols (+0.5, +1.5), Faltas (Mais de X faltas), Cartões (+1.5 cartões) ou Chutes ao Alvo.
        3. NOMES REAIS: Use APENAS os nomes dos times enviados na lista. Proibido usar "Time Casa" ou "Exemplo".
        4. EXATIDÃO: Responda APENAS as 6 linhas de dados, separadas por |.

        FORMATO: Jogo X|Time A x Time B|Mercado Selecionado|Justificativa Estatística|Odd Estimada
        """
    else:
        prompt_odd_ouro = f"""Aja como um analista esportivo de elite. Escolha EXATAMENTE 6 entradas de alta segurança:
        {lista_jogos_com_stats}

        REGRAS:
        1. ESTRUTURA BLINDADA DO BILHETE: O bilhete DEVE conter EXATAMENTE 2 (DUAS) "Vitória Seca" (dos super favoritos jogando em casa) e EXATAMENTE 1 (UMA) "Dupla Chance" (Favorito ou Empate). Nenhuma outra aposta de resultado (vitória ou empate) é permitida.
        2. RESTANTE 100% ESTATÍSTICO: Todas as outras entradas (para preencher o bilhete) DEVEM ser EXCLUSIVAMENTE estatísticas de altíssima probabilidade: 'Mais de 0.5 Gols na Partida', 'Mais de 1.5 Gols', 'Mais de 4.5 Escanteios no Jogo', 'Mais de 0.5 Gols no 1º Tempo', 'Mais de 2.5 GOLS' ou 'Menos de 26.5 Faltas Totais'.
        3. PROIBIÇÃO: Nunca use nomes genéricos como "Time Casa" ou "Time A".
        4. QUANTIDADE LIVRE E ODD TOTAL OBRIGATÓRIA (5.00 A 10.00): Adicione quantas entradas estatísticas forem necessárias usando odds individuais absurdamente baixas/seguras. Você é OBRIGADO a acumular essas entradas até que o resultado final (Odd Total) ultrapasse 5.00. Nunca entregue um bilhete com odd final abaixo de 5.00 ou acima de 10.00.

        FORMATO: Jogo X|Time A x Time B|Mercado Selecionado|Justificativa Estatística|Odd Estimada
        """

    try:
        resultado_bruto = processar_analise_chat(prompt_odd_ouro)

        linhas = resultado_bruto.replace('*', '').strip().split('\n')
        html_final = '<div style="text-align: left; margin-top: 10px;">'
        texto_zap = "*ODD DE OURO - FLUXOBET*\n\n"
        odd_total = 1.0
        jogos_encontrados = 0

        for linha in linhas:
            partes = linha.split('|')
            if len(partes) >= 5:
                nome_jogo = partes[1].strip()

                if any(x in nome_jogo.upper() for x in ["TIME CASA", "TIME FORA", "TIME A", "TIME B", "MANDANTE", "EXEMPLO"]):
                    continue

                mercado = partes[2].strip()
                justificativa = partes[3].strip()
                odd_str = partes[4].replace(',', '.').strip()

                try:
                    odd = float(re.findall(r"\d+\.\d+|\d+", odd_str)[0])
                except Exception:
                    odd = 1.55

                odd_total *= odd
                jogos_encontrados += 1

                html_final += f"""
                <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,215,0,0.2); border-radius: 12px; padding: 15px; margin-bottom: 15px;">
                    <div style="color: #FFD700; font-weight: 800; font-size: 1rem; margin-bottom: 8px; text-transform: uppercase;">{nome_jogo}</div>
                    <div style="color: #fff; font-size: 0.9rem; margin-bottom: 5px;"><strong>Entrada:</strong> <span style="color: #d1d5db;">{mercado}</span></div>
                    <div style="color: #9ca3af; font-size: 0.8rem; line-height: 1.4;"><strong>Análise:</strong> {justificativa}</div>
                    <div style="color: #22c55e; font-weight: bold; margin-top: 10px; font-size: 0.9rem;">Odd: {odd:.2f}</div>
                </div>
                """
                texto_zap += f"{nome_jogo}\n{mercado}\nOdd: {odd:.2f}\n\n"

        limite_minimo = 4 if num_jogos < 6 else 5
        if jogos_encontrados < limite_minimo:
            raise ValueError(f"IA gerou apenas {jogos_encontrados} entradas válidas.")

        odd_total = round(odd_total, 2)
        html_final += f"""
            <div style="text-align: center; margin-top: 20px; padding-top: 15px; border-top: 1px dashed rgba(255,215,0,0.3);">
                <h4 style="color: #FFD700; font-weight: 900; margin: 0 0 15px 0;">ODD TOTAL: {odd_total}</h4>
                <button class="btn w-100" style="background: #25D366; color: white; font-weight: bold; padding: 12px;" onclick="copiarBilheteIA()">
                    <i class="bi bi-whatsapp me-2"></i> Copiar para WhatsApp
                </button>
            </div>
        </div>
        """

        texto_zap += f"*ODD TOTAL ESTIMADA: {odd_total}*\n\nGerado pelo Robô FluxoBet VIP"

        resposta_final = {
            "status": "success",
            "mensagem_html": html_final,
            "texto_copiar": texto_zap
        }

        dados_salvar = {'dados': resposta_final, 'tempo': tempo_atual}
        try:
            with open(caminho_cache_ouro, 'w', encoding='utf-8') as f:
                json.dump(dados_salvar, f, ensure_ascii=False)
        except Exception as e:
            print(f"Erro ao salvar arquivo cache_ouro.json: {e}")

        CACHE_BILHETES['ouro'] = dados_salvar

        return jsonify(resposta_final)

    except Exception as e:
        print(f"Erro Odd de Ouro: {e}")
        return jsonify({
            "status": "error",
            "mensagem": "Nossos analistas estão recalculando as probabilidades. Tente novamente em 1 minuto."
        }), 500


# ==========================================
# rotas de pagamento do banco assas 
# ==========================================

@app.route('/checkout/<plano_escolhido>')
def checkout(plano_escolhido):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    plano_escolhido = plano_escolhido if plano_escolhido in PLANOS else 'mensal'
    return render_template('checkout.html', user=User.query.get(session['user_id']), plano=plano_escolhido, detalhes=PLANOS[plano_escolhido])


@app.route('/api/processar-pagamento', methods=['POST'])
def processar_pagamento():
    if 'user_id' not in session:
        return jsonify({"erro": "Não autorizado. Faça login novamente."}), 401

    dados = request.json
    plano_key = dados.get('plano', 'mensal')
    usuario = User.query.get(session['user_id'])

    if not usuario:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    plano = PLANOS.get(plano_key)
    if not plano:
        return jsonify({"erro": "Plano inválido."}), 400

    headers = {
        "access_token": ASAAS_API_KEY,
        "Content-Type": "application/json"
    }

    cpf_limpo = ''.join(filter(str.isdigit, usuario.cpf))

    try:
        cliente_payload = {
            "name": usuario.nome,
            "cpfCnpj": cpf_limpo,
            "email": usuario.email,
            "mobilePhone": usuario.telefone
        }
        res_cliente = requests.post(f"{URL_ASAAS}/customers", json=cliente_payload, headers=headers).json()

        customer_id = res_cliente.get('id')
        if not customer_id and 'errors' in res_cliente:
            print(f"Erro cliente Asaas: {res_cliente}")
            return jsonify({"erro": "Erro ao validar CPF/Dados no banco."}), 400

        vencimento = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')
        reference_id = f"PEDIDO_{usuario.id}_{uuid.uuid4().hex[:8]}"

        cobranca_payload = {
            "customer": customer_id,
            "billingType": "PIX",
            "value": plano['valor'] / 100,
            "dueDate": vencimento,
            "description": plano['nome'],
            "externalReference": reference_id
        }

        res_cobranca = requests.post(f"{URL_ASAAS}/payments", json=cobranca_payload, headers=headers)

        if res_cobranca.status_code == 200:
            dados_cobranca = res_cobranca.json()
            return jsonify({"status": "redirecionar", "url": dados_cobranca.get('invoiceUrl')})

        print(f"Erro cobrança Asaas: {res_cobranca.json()}")
        return jsonify({"erro": "Erro ao gerar cobrança"}), 400

    except Exception as e:
        print(f"Erro interno no pagamento: {str(e)}")
        return jsonify({"erro": "Erro de conexão com o Banco"}), 500


@app.route('/api/verificar-vip')
def verificar_vip():
    if 'user_id' not in session:
        return jsonify({"vip": False}), 401

    usuario = User.query.get(session['user_id'])
    if usuario and usuario.vip:
        session['plano_ativo'] = True
        return jsonify({"vip": True})

    return jsonify({"vip": False})


@app.route('/sucesso')
def sucesso():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    usuario = User.query.get(session['user_id'])

    if not usuario.vip:
        return redirect(url_for('perfil'))

    return render_template('sucesso.html', user=usuario)


@app.route('/webhook/asaas', methods=['POST'])
def webhook_asaas():
    # Validação obrigatória do token de segurança configurado no painel do Asaas
    token_recebido = request.headers.get('asaas-access-token')
    if token_recebido != WEBHOOK_SECRET_TOKEN:
        return jsonify({"erro": "Não autorizado"}), 403

    dados = request.json
    if not dados:
        return jsonify({"status": "ignorado"}), 200

    evento = dados.get('event')
    if evento in ['PAYMENT_RECEIVED', 'PAYMENT_CONFIRMED']:
        pagamento = dados.get('payment', {})
        reference_id = pagamento.get('externalReference')
        status = pagamento.get('status')

        if status in ['RECEIVED', 'CONFIRMED'] and reference_id:
            try:
                partes_ref = str(reference_id).split('_')
                if len(partes_ref) > 1 and partes_ref[1].isdigit():
                    usuario_id = int(partes_ref[1])
                    usuario = User.query.get(usuario_id)

                    if usuario:
                        usuario.vip = True

                        if usuario.indicado_por:
                            afiliado = User.query.get(usuario.indicado_por)
                            if afiliado:
                                afiliado.saldo_comissao += 5.00

                        db.session.commit()
                        print(f"VIP ativado para: {usuario.nome}")
            except Exception as e:
                print(f"Erro no banco no webhook: {e}")

    return jsonify({"status": "recebido"}), 200


# ==========================================
# area administrativa 
# ==========================================

def admin_blindado(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"status": "error", "mensagem": "Sessão expirada."}), 401
            return abort(404)

        user = User.query.get(session['user_id'])
        if not user or not user.is_admin or not session.get('admin_autenticado'):
            if request.is_json:
                return jsonify({"status": "error", "mensagem": "Acesso negado."}), 403
            return abort(404)

        return f(*args, **kwargs)
    return decorated_function


@app.route('/fluxo-control', methods=['GET', 'POST'])
def login_admin_secreto():
    if 'user_id' not in session:
        return abort(404)
    user = User.query.get(session['user_id'])
    if not user or not user.is_admin:
        return abort(404)

    if request.method == 'POST':
        senha_digitada = request.form.get('master_key')
        if senha_digitada == ADMIN_MASTER_KEY:
            session['admin_autenticado'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template('login_admin.html', erro="Chave Mestra Incorreta.")

    return render_template('login_admin.html')


@app.route('/admin-center')
@admin_blindado
def admin_dashboard():
    usuarios = User.query.order_by(User.id.desc()).all()
    return render_template('admin_dashboard.html', usuarios=usuarios)


@app.route('/admin/toggle-vip/<int:id>', methods=['GET', 'POST'])
@app.route('/admin/toggle_vip/<int:id>', methods=['GET', 'POST'])
@admin_blindado
def admin_toggle_vip(id):
    try:
        user = User.query.get_or_404(id)
        user.vip = not user.vip
        db.session.commit()

        if request.method == 'GET':
            return redirect(url_for('admin_dashboard'))

        return jsonify({"status": "success", "vip": user.vip})
    except Exception as e:
        print(f"Erro no VIP: {e}")
        return jsonify({"status": "error"}), 500


@app.route('/admin/delete-user/<int:id>', methods=['POST'])
@admin_blindado
def admin_delete_user(id):
    try:
        user = User.query.get_or_404(id)

        if user.id == session.get('user_id'):
            return jsonify({"status": "error", "mensagem": "Você não pode se auto-banir!"}), 400

        DeviceLog.query.filter_by(user_id=id).delete()
        ChatUsage.query.filter_by(user_id=id).delete()
        TicketUsage.query.filter_by(user_id=id).delete()

        db.session.delete(user)
        db.session.commit()

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "mensagem": str(e)}), 500


# ==========================================
# rotas de manutenção (somente admin)
# ==========================================

@app.route('/forcar-atualizacao')
@admin_blindado
def forcar_atualizacao():
    try:
        scanner()
        return redirect(url_for('index'))
    except Exception as e:
        return f"<h1>Erro ao forçar atualização:</h1><p>{str(e)}</p>"


@app.route('/gerar-site-agora')
@admin_blindado
def gerar_site_agora():
    global CACHE_JOGOS, DADOS_CACHE

    CACHE_JOGOS["dados"] = []
    CACHE_JOGOS["ultima_atualizacao"] = None

    try:
        novos_dados = buscar_jogos_do_dia()

        if novos_dados and len(novos_dados) > 0:
            DADOS_CACHE = novos_dados

            caminho = os.path.join(basedir, 'jogos_cache.json')
            with open(caminho, 'w', encoding='utf-8') as f:
                json.dump(novos_dados, f, ensure_ascii=False)

            return (
                f"<h1>Sucesso!</h1><p>O robô foi na API e salvou <b>{len(novos_dados)} jogos</b> para hoje.</p>"
                f"<br><a href='/'>Ver jogos no site</a>"
            )
        else:
            return "<h1>Lista vazia</h1><p>O robô rodou sem erros, mas não encontrou nenhum jogo para hoje.</p>"

    except Exception as e:
        return f"<h1>Erro:</h1><p>{str(e)}</p>"


if __name__ == '__main__':
    app.run(debug=False)
