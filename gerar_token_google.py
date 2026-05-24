#!/usr/bin/env python3
"""
gerar_token_google.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Script de USO ÚNICO — rode localmente para obter o refresh_token
que permite ao GitHub Actions fazer upload no seu Google Drive.

PRÉ-REQUISITOS (instale uma vez):
    pip install google-auth-oauthlib

COMO USAR:
    1. No Google Cloud Console, crie credenciais OAuth2:
       APIs e Serviços → Credenciais → Criar credenciais
       → ID do cliente OAuth → Tipo: Aplicativo de desktop
       → Baixe o JSON ou copie o client_id e client_secret

    2. Execute este script:
       python gerar_token_google.py

    3. Um navegador vai abrir pedindo autorização — aceite.

    4. Copie os 3 valores exibidos no terminal para os Secrets
       do GitHub (Settings → Secrets and variables → Actions).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit(
        "[ERRO] Dependência ausente.\n"
        "Execute: pip install google-auth-oauthlib\n"
        "e rode o script novamente."
    )

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  Gerador de Refresh Token — Google Drive OAuth2")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    # Verifica se um arquivo credentials.json foi baixado do Console
    creds_file = Path("credentials.json")
    if creds_file.exists():
        print(f"[OK] Usando {creds_file} encontrado na pasta atual.")
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    else:
        print("Arquivo credentials.json não encontrado.")
        print("Cole os dados abaixo (obtidos no Google Cloud Console):\n")
        client_id     = input("  client_id     : ").strip()
        client_secret = input("  client_secret : ").strip()

        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id":      client_id,
                    "client_secret":  client_secret,
                    "auth_uri":       "https://accounts.google.com/o/oauth2/auth",
                    "token_uri":      "https://oauth2.googleapis.com/token",
                    "redirect_uris":  ["http://localhost"],
                }
            },
            scopes=SCOPES,
        )

    print("\n[>>>] Abrindo navegador para autorização...")
    print("      Faça login com a conta Google dona da pasta no Drive.\n")

    creds = flow.run_local_server(port=0, open_browser=True)

    # Extrai client_id e client_secret do flow para exibir
    if creds_file.exists():
        with open(creds_file) as f:
            data = json.load(f)
        client_id     = data["installed"]["client_id"]
        client_secret = data["installed"]["client_secret"]

    print("\n" + "━" * 54)
    print("  ✅ AUTORIZAÇÃO CONCLUÍDA!")
    print("━" * 54)
    print("\nCopie os 3 valores abaixo para os GitHub Secrets:\n")
    print(f"  GOOGLE_CLIENT_ID     →  {client_id}")
    print(f"  GOOGLE_CLIENT_SECRET →  {client_secret}")
    print(f"  GOOGLE_REFRESH_TOKEN →  {creds.refresh_token}")
    print("\n" + "━" * 54)
    print("\n⚠️  Remova o secret GOOGLE_SERVICE_ACCOUNT_JSON do GitHub")
    print("   (ele não é mais necessário).\n")


if __name__ == "__main__":
    main()
