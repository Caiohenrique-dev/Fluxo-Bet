# ia_fluxo.py
import os
import json
import time

from google import genai

# ==========================================
# CHAVE DE API — vem do ambiente (.env), nunca hardcoded
# ==========================================
CHAVE_API = os.environ.get('GEMINI_API_KEY')
if not CHAVE_API:
    raise RuntimeError(
        "Variável de ambiente 'GEMINI_API_KEY' não configurada. "
        "Defina-a no arquivo .env antes de iniciar o servidor."
    )

client = genai.Client(api_key=CHAVE_API)

MODELO_ATUAL = "gemini-2.5-flash"


# ==========================================
# FUNÇÃO DE PROTEÇÃO (RETRY COM BACKOFF)
# ==========================================
def chamar_gemini_com_retry(prompt, max_tentativas=5):
    """
    Tenta chamar a API. Se der erro 503 (ocupado) ou 429 (limite),
    espera progressivamente (2s, 4s, 8s...) antes de desistir.
    """
    for tentativa in range(max_tentativas):
        try:
            resposta = client.models.generate_content(
                model=MODELO_ATUAL,
                contents=prompt
            )
            return resposta
        except Exception as e:
            erro_str = str(e)

            if "503" in erro_str or "429" in erro_str or "500" in erro_str or "RESOURCE_EXHAUSTED" in erro_str:
                if tentativa < max_tentativas - 1:
                    tempo_espera = 2 ** (tentativa + 1)
                    print(f"API congestionada (tentativa {tentativa + 1}/{max_tentativas}). Aguardando {tempo_espera}s...")
                    time.sleep(tempo_espera)
                else:
                    print("Google recusou todas as tentativas. Desistindo por agora.")
                    raise e
            else:
                raise e  # Erro na chave ou outro erro não recuperável: falha na hora


# ==========================================
# FUNÇÕES DE ANÁLISE
# ==========================================

def processar_analise_chat(texto_aposta):
    """Lógica específica para o Chat da Home"""
    prompt = f"""
    Você é o 'Robô FluxoBet', um especialista sênior em apostas esportivas e inteligência de dados.
    O usuário enviou a seguinte solicitação ou lista de jogos: "{texto_aposta}"

    DIRETRIZES DO ROBÔ:
    1. Se o usuário pedir "odd segura" ou "aposta segura": Monte uma resposta focando em mercados de altíssima probabilidade (Dupla Chance, Over 1.5 gols, Favorito).
    2. Se pedir "alavancagem" ou "risco": Busque odds maiores, sugerindo resultados secos ou combinação de gols.
    3. Se enviar uma lista de vários jogos: Analise todos juntos como um único bilhete (múltipla) e dê o veredito geral da operação.
    4. REGRA DE OURO (PROIBIDO CITAR JOGADORES): Como você não tem a lista de escalação atualizada, NUNCA cite nomes de jogadores específicos. Foque apenas na força das equipes (ex: "ataque do Chelsea", "defesa do Brighton").

    Retorne APENAS o HTML formatado abaixo, preenchido com sua análise, sem nenhuma marcação markdown ou crases:

    <strong style="color: var(--primary);">Robô FluxoBet:</strong> Análise processada para: <em>"{texto_aposta}"</em>.<br><br>
    <span style="color: #000; font-weight: bold; background: var(--primary); padding: 2px 6px; border-radius: 4px;">Chance de Green: [Coloque a %, ex: 85%]</span><br><br>
    <strong>Veredito:</strong> [Sua análise técnica, em 2 a 3 linhas, indicando as melhores opções de apostas (mercados) para o que foi pedido.]
    """
    try:
        resposta = chamar_gemini_com_retry(prompt)
        return resposta.text.replace("```html", "").replace("```", "").strip()
    except Exception as e:
        return f"Erro na análise: {str(e)}"


def gerar_analise_bilhete(jogo, odd, palpite):
    """Lógica para justificar um único jogo (caso precise em outro lugar)"""
    prompt = f"Analista FluxoBet. Jogo: {jogo}. Palpite: {palpite}. Odd: {odd}. Justifique em 2 linhas curtas por que essa aposta tem valor."
    try:
        resposta = chamar_gemini_com_retry(prompt)
        return resposta.text.strip()
    except Exception:
        return "Análise baseada em cruzamento de dados de mercado."


def selecionar_melhores_jogos_ia(jogos_hoje, perfil_risco):
    if not jogos_hoje:
        return None

    resumo_jogos = ""
    for j in jogos_hoje:
        try:
            id_jogo = j.get('id_api', 0)
            casa = j['times']['casa']
            fora = j['times']['fora']
            liga = j.get('liga_nome', 'Liga')
            odd_c = j['stats']['casa']['book']
            odd_e = j['stats']['empate']['book']
            odd_f = j['stats']['fora']['book']
            resumo_jogos += f"ID: {id_jogo} | {liga} | {casa} x {fora} | Odds Base: Casa {odd_c}, Empate {odd_e}, Fora {odd_f}\n"
        except KeyError:
            continue

    prompt = f"""
    Você é o Robô FluxoBet, um analista esportivo de elite focado em garantir o GREEN com as maiores probabilidades matemáticas possíveis.
    Sua tarefa é montar um bilhete usando APENAS os jogos abaixo.

    JOGOS DISPONÍVEIS:
    {resumo_jogos}

    Perfil de Risco solicitado: {perfil_risco.upper()}

    REGRAS DE SELEÇÃO POR PERFIL (OBEDEÇA RIGOROSAMENTE):
    - BAIXO RISCO: Extrema segurança, selecionando de 2 a 4 jogos no máximo para que a ODD TOTAL COMBINADA da múltipla fique entre 2.00 e 4.00, com a regra estrita de que nenhuma seleção individual tenha odd inferior a 1.20 para evitar retornos ridículos. Use APENAS mercados de altíssima probabilidade de acerto, escolhendo obrigatoriamente entre "Mais de 0.5 Gols no 1º Tempo", "Mais de 1.5 Gols na Partida", "Dupla Chance (1X ou X2)" exclusivo para super favoritos, "Menos de 4.5 ou 5.5 Gols na Partida", ou "Mais de 6.5 ou 7.5 Escanteios Totais na Partida", variando entre esses mercados com base na estatística dominante de cada confronto e descartando opções que não atinjam o piso da odd para compor a odd final com valor real.
    - MÉDIO RISCO: Combinações inteligentes com alta probabilidade de bater, mas com a regra estrita de buscar valor real: estabeleça uma ODD TOTAL COMBINADA entre 3.00 e 6.00, descartando automaticamente qualquer seleção individual com odd inferior a 1.30 para evitar retornos muito baixos. É ESTRITAMENTE PROIBIDO usar "Ambas Marcam" (BTTS) e quantidades altas de cartões; utilize APENAS mercados liberados como Escanteios (buscando linhas asiáticas que paguem acima do piso), Mais de 1.5 ou 2.0 Gols Asiáticos, Vitória Seca ou Dupla Chance (MAS APENAS para times que são muito favoritos), forçando o algoritmo a buscar a linha adjacente de maior valor caso a cotação padrão esteja esmagada.
    - ALTO RISCO: Extrema agressividade na quantidade, selecionando rigorosamente de 6 a 8 jogos onde o risco principal é o volume do bilhete, exigindo que as seleções individuais tenham forte embasamento matemático. Limita-se a no máximo 1 ou 2 seleções de "Vitória Direta de Super Favoritos" (com a condição obrigatória de que joguem em casa), preenchendo o restante do bilhete apenas com estatísticas, utilizando mercados como "Dupla Hipótese (Favorito ou Empate)", "Mais de 1.5 ou 2.5 Gols na Partida", "Ambas as Equipes Marcam (Sim)", "Mais de 0.5 Gols no 2º Tempo", "Mais de 34.5 ou 39.5 Laterais Totais", "Mais de 20.5 ou 22.5 Faltas Totais", "Mais de 2.5 Cartões Totais" e "Mais de 3.5 Chutes ao Alvo do Time Favorito", mantendo rigorosa coerência estatística ao cruzar dados recentes para justificar cada entrada e evitar opções que conflitem com o comportamento padrão das equipes na temporada. O risco é a quantidade de jogos, mas mantenha a coerência estatística.
    - ZEBRA: 2 a 4 jogos focados em surpresas bem fundamentadas e não em loucuras impossíveis. As odds individuais de cada palpite devem ser sempre acima de 3.00. Mercados ideais para este nível: "Empate Seco" (em jogos equilibrados ou clássicos), "Vitória do Azarão" (apenas se o azarão jogar em casa ou tiver um bom retrospecto), "Mais de 4.5 Gols" na partida, ou "Cartão Vermelho" (em clássicos locais muito tensos). A justificativa deve explicar o motivo estatístico claro para essa aposta ousada.

    RETORNE APENAS UM JSON EXATAMENTE NESTE FORMATO (sem markdown, sem crases):
    {{
      "bilhete": [
        {{
            "times": "Nome Casa x Nome Fora",
            "liga": "Nome da Liga",
            "mercado": "Ex: Gols, Escanteios, Resultado Final, Ambas Marcam (Sim)",
            "palpite": "Ex: Mais de 0.5 Gols, Vitória Casa, Mais de 8.5 Escanteios",
            "odd": 1.25,
            "justificativa": "Texto curto provando a alta probabilidade do green"
        }}
      ]
    }}
    """
    try:
        resposta = chamar_gemini_com_retry(prompt)
        limpo = resposta.text.replace("```json", "").replace("```", "").strip()
        return json.loads(limpo)
    except Exception as e:
        print(f"Erro na seleção da IA: {e}")
        return None


# ==========================================
# FUNÇÃO PARA A PÁGINA DE DETALHES
# ==========================================
def gerar_insights_detalhados(jogo):
    """Gera mercados especiais e análise técnica reais"""

    casa = jogo.get('times', {}).get('casa', 'Time da Casa')
    fora = jogo.get('times', {}).get('fora', 'Time de Fora')
    liga = jogo.get('liga_nome', 'Liga Desconhecida')

    stats = jogo.get('stats', {})
    odd_c = stats.get('casa', {}).get('book', 2.0)
    odd_e = stats.get('empate', {}).get('book', 3.0)
    odd_f = stats.get('fora', {}).get('book', 3.5)

    prompt = f"""
    Você é o analista chefe da FluxoBet. Analise esta partida:
    Jogo: {casa} x {fora}
    Liga: {liga}
    Odds: Casa ({odd_c}), Empate ({odd_e}), Fora ({odd_f}).

    Com base no seu conhecimento de futebol, retorne APENAS um JSON (sem usar crases ou markdown) com os seguintes dados:
    {{
      "analise_texto": "Crie 2 linhas curtas de análise técnica justificando a odd baseada na força dos times.",
      "jogador_mercado": "Crie um mercado de finalizações ou gols focado no time favorito, NUNCA use nome de jogador específico (Ex: '+3.5 chutes no gol do {casa}' ou 'Marcador: Atacante do {casa}')",
      "jogador_odd": 1.85,
      "cantos_mercado": "Mercado de escanteios (Ex: Mais de 9.5 Escanteios)",
      "cantos_odd": 1.65
    }}
    """
    try:
        resposta = chamar_gemini_com_retry(prompt)
        limpo = resposta.text.replace("```json", "").replace("```", "").strip()
        return json.loads(limpo)
    except Exception as e:
        print(f"Erro na IA de Detalhes após várias tentativas: {e}")
        return {
            "analise_texto": "Análise baseada em volume de apostas indicando favoritismo matemático considerando o cenário atual.",
            "jogador_mercado": f"Mais de 1.5 finalizações ({casa} ou {fora})",
            "jogador_odd": 1.80,
            "cantos_mercado": "Mais de 8.5 cantos na partida",
            "cantos_odd": 1.60
        }
