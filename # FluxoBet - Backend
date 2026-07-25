# FluxoBet

## Antes de rodar

1. Copie `.env.example` para `.env`:
   ```bash
   cp .env.example .env
   ```
2. Preencha o `.env` com suas chaves reais (nunca commite esse arquivo).
3. Instale as dependências:
   ```bash
   pip install flask flask_sqlalchemy flask_limiter apscheduler scipy python-dotenv itsdangerous requests google-genai
   ```
4. Rode o app:
   ```bash
   python app.py
   ```

## Estrutura

- `app.py` — aplicação Flask principal (rotas, banco de dados, pagamentos)
- `ia_fluxo.py` — integrações com a IA (Google Gemini) para análises e bilhetes
- `config_ligas.py` — configuração estática das ligas de futebol
- `templates/` — (adicionar) templates HTML do site
- `static/` — (adicionar) arquivos estáticos (CSS, JS, imagens)

## Segurança

- Nunca commite o arquivo `.env` real.
- As variáveis `FLUXO_SECRET_KEY`, `FLUXO_ADMIN_KEY` e `WEBHOOK_TOKEN` não têm valor padrão — o app falha ao iniciar se elas não estiverem definidas, de propósito.
- Se qualquer chave de API ou segredo já foi exposta publicamente (ex: em um chat, commit antigo, etc.), revogue e gere uma nova antes de colocar em produção.
