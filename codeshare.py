#!/usr/bin/env python3
"""Remplace le texte d'un document Codeshare avec un fichier local.

Utilisation : python3 codeshare_send.py test.txt https://codeshare.io/aVE8dR
Seuls Python 3, sa bibliothèque standard et curl sont nécessaires.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


# Codeshare refuse l'agent utilisateur par défaut de curl. Cet en-tête HTTP
# permet de recevoir la page HTML sans lancer de navigateur.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Cette mutation est celle utilisée par l'éditeur Codeshare pour enregistrer
# une opération de texte. Son schéma peut changer si Codeshare évolue.
CREATE_HISTORY = """mutation createCodeHistoryMutation($input: CreateCodeHistoryInput!) {
  createCodeHistory(input: $input) {
    codeshare { id }
    newCodeHistoryEdge { node { id historyId value } }
  }
}"""

# La lecture paginée inclut le checkpoint et toutes les opérations suivantes.
READ_DOCUMENT = """query ReadCodeshare($id: String!, $after: String) {
  codeshare(id: $id) {
    id
    migratedAt
    canEdit { me }
    codeCheckpoint { codeHistoryId value }
    codeHistory(first: 1000, after: $after) {
      edges { node { historyId value } }
      pageInfo { endCursor hasNextPage }
    }
  }
}"""


def curl(args, *, body=None):
    """Exécute curl sans shell et signale les erreurs HTTP ou réseau."""
    result = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "30", *args],
        input=body,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode("utf-8")


def open_session(url, cookie_file):
    """Ouvre une session anonyme et récupère son jeton depuis __NEXT_DATA__."""
    html = curl(["--user-agent", USER_AGENT, "--cookie-jar", str(cookie_file), url])
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("La page Codeshare ne contient pas l'état initial attendu.")
    page = json.loads(match.group(1))["props"]["pageProps"]
    return page["codeshareId"], page["apiHeaders"]["authorization"]


def graphql(query, variables, token, cookie_file):
    """Envoie une requête GraphQL avec la même session que la page HTML."""
    payload = json.dumps({"query": query, "variables": variables}, ensure_ascii=False).encode("utf-8")
    response = curl(
        [
            "--user-agent", USER_AGENT,
            "--cookie", str(cookie_file),
            "--header", "Content-Type: application/json",
            "--header", "Authorization: " + token,
            "--header", "x-codeshare-auth-mutation: true",
            "--data-binary", "@-",
            "https://codeshare.io/-/api/graphql",
        ],
        body=payload,
    )
    result = json.loads(response)
    if result.get("errors"):
        messages = "; ".join(error.get("message", "Erreur GraphQL") for error in result["errors"])
        raise RuntimeError(messages)
    return result["data"]


def read_document(codeshare_id, token, cookie_file):
    """Lit toutes les pages de l'historique et garde l'état du document."""
    after = None
    history = []
    document = None
    while True:
        result = graphql(
            READ_DOCUMENT,
            {"id": codeshare_id, "after": after},
            token,
            cookie_file,
        )
        current = result.get("codeshare")
        if current is None:
            raise RuntimeError("Document Codeshare introuvable.")
        if document is None:
            document = current
        history.extend(edge["node"] for edge in current["codeHistory"]["edges"] if edge["node"])
        info = current["codeHistory"]["pageInfo"]
        if not info["hasNextPage"]:
            break
        if not info["endCursor"] or info["endCursor"] == after:
            raise RuntimeError("Pagination de l'historique Codeshare incohérente.")
        after = info["endCursor"]
    return document, history


def decode_history_id(value):
    """Décodage de l'index base62 préfixé utilisé par Codeshare."""
    if not value or value[0] not in ALPHABET:
        raise ValueError("Identifiant d'historique invalide.")
    length = ALPHABET.index(value[0]) - 9
    if length != len(value) - 1 or length < 1:
        raise ValueError("Longueur d'identifiant d'historique invalide.")
    number = 0
    for digit in value[1:]:
        number = number * 62 + ALPHABET.index(digit)
    return number


def encode_history_id(number):
    """Crée l'identifiant qui suit la dernière opération Codeshare."""
    digits = "0"
    if number:
        digits = ""
        while number:
            number, remainder = divmod(number, 62)
            digits = ALPHABET[remainder] + digits
    return ALPHABET[len(digits) + 9] + digits


def utf16_bytes(value):
    """Compte les caractères comme JavaScript, en unités UTF-16."""
    return value.encode("utf-16-le", "surrogatepass")


def apply_operation(text, operation):
    """Applique retain, delete et insert selon le format OT de Codeshare."""
    source = utf16_bytes(text)
    output = bytearray()
    offset = 0
    for part in operation:
        if isinstance(part, str):
            output.extend(utf16_bytes(part))
        elif type(part) is int and part > 0:
            end = offset + part * 2
            if end > len(source):
                raise ValueError("Opération retain hors du document.")
            output.extend(source[offset:end])
            offset = end
        elif type(part) is int and part < 0:
            offset += -part * 2
            if offset > len(source):
                raise ValueError("Opération delete hors du document.")
        else:
            raise ValueError("Opération de texte Codeshare invalide.")
    if offset != len(source):
        raise ValueError("Opération de texte incomplète.")
    return output.decode("utf-16-le", "surrogatepass")


def checkpoint_index(checkpoint):
    """Récupère l'index du dernier historique couvert par le checkpoint."""
    raw = base64.b64decode(checkpoint["codeHistoryId"]).decode("utf-8")
    return decode_history_id(raw.rsplit(":", 1)[-1])


def current_text(document, history):
    """Reconstruit le texte et son dernier numéro d'historique."""
    checkpoint = document["codeCheckpoint"]
    text = apply_operation("", checkpoint["value"]) if checkpoint else ""
    previous = checkpoint_index(checkpoint) if checkpoint else -1
    for entry in history:
        number = decode_history_id(entry["historyId"])
        if number <= previous:
            continue
        if number != previous + 1:
            raise ValueError("Historique incomplet ou désordonné.")
        text = apply_operation(text, entry["value"])
        previous = number
    return text, previous


def replacement_operation(old_text, new_text):
    """Construit une seule opération OT qui remplace le texte intégral."""
    count = len(utf16_bytes(old_text)) // 2
    operation = [-count] if count else []
    if new_text:
        operation.append(new_text)
    return operation


def main():
    """Lit le fichier, publie sa valeur et confirme la valeur enregistrée."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Fichier texte UTF-8 à publier")
    parser.add_argument("url", help="URL du document Codeshare existant")
    parser.add_argument("--dry-run", action="store_true", help="Vérifie l'accès sans publier")
    args = parser.parse_args()

    parsed = urlparse(args.url)
    if parsed.scheme != "https" or parsed.netloc != "codeshare.io" or not re.fullmatch(r"/[A-Za-z0-9]+", parsed.path):
        parser.error("L'URL doit être de la forme https://codeshare.io/IDENTIFIANT")
    desired = args.file.read_text(encoding="utf-8")

    # Le fichier de cookies reste dans un répertoire temporaire privé et n'est
    # jamais imprimé. Le jeton de session n'est conservé qu'en mémoire.
    with tempfile.TemporaryDirectory(prefix="codeshare-send-") as temp_dir:
        cookie_file = Path(temp_dir) / "cookies"
        codeshare_id, token = open_session(args.url, cookie_file)
        document, history = read_document(codeshare_id, token, cookie_file)
        if document["migratedAt"]:
            raise RuntimeError("Ce document utilise un nouveau protocole Codeshare non pris en charge.")
        if not document["canEdit"]["me"]:
            raise RuntimeError("La session anonyme ne peut pas modifier ce document.")
        actual, last_index = current_text(document, history)
        if actual == desired:
            print("Le document contient déjà exactement le contenu du fichier.")
            return
        if args.dry_run:
            print(f"Accès validé : {len(utf16_bytes(actual)) // 2} caractères UTF-16 actuels, "
                  f"{len(utf16_bytes(desired)) // 2} à publier.")
            return

        # Une opération par publication permet au serveur de rejeter un index
        # occupé si un autre collaborateur a écrit entre la lecture et l'envoi.
        operation = replacement_operation(actual, desired)
        result = graphql(
            CREATE_HISTORY,
            {"input": {"codeshareId": codeshare_id,
                       "historyId": encode_history_id(last_index + 1),
                       "value": operation}},
            token,
            cookie_file,
        )
        if not (result.get("createCodeHistory") or {}).get("newCodeHistoryEdge"):
            raise RuntimeError("Codeshare n'a pas confirmé l'enregistrement.")

        # La lecture finale vérifie le contenu distant, et non uniquement la
        # réponse de la mutation. Elle détecte aussi une édition concurrente.
        updated_document, updated_history = read_document(codeshare_id, token, cookie_file)
        updated_text, _ = current_text(updated_document, updated_history)
        if updated_text != desired:
            raise RuntimeError("Envoi accepté, mais le document distant diffère du fichier.")
        print(f"Contenu de {args.file} publié et vérifié sur {args.url}.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        sys.exit(1)
