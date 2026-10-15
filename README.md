# Envoi d'un fichier vers Codeshare

Ce projet permet de remplacer le contenu d'un document collaboratif Codeshare par celui d'un fichier texte local, depuis un terminal Linux. Aucun navigateur ni environnement graphique n'est nécessaire.

## Script Python

### Prérequis

- Python 3 et `curl` déjà présents sur le serveur ; aucune bibliothèque Python à installer.
- Un fichier texte encodé en UTF-8.
- Un lien Codeshare dont l'édition est autorisée pour la session utilisée.

### Utilisation

Depuis le dossier contenant `codeshare_send.py` :

```bash
python3 codeshare_send.py test.txt https://codeshare.io/aVE8dR
```

Le **premier argument** est le fichier à envoyer : remplacez `test.txt` par le nom ou le chemin de votre fichier. Le **second argument** est l'URL du document Codeshare existant.

Pour vérifier l'accès et calculer la taille du texte sans modifier le document :

```bash
python3 codeshare_send.py test.txt https://codeshare.io/aVE8dR --dry-run
```

Le script ouvre une session anonyme, lit l'état du document, envoie une opération qui **remplace tout son contenu**, puis relit le résultat pour confirmer l'envoi. Si le contenu est déjà identique au fichier, il ne crée aucune modification.

## Variante Bash

Une version sans Python est disponible si `bash`, `curl` et `jq` sont déjà installés :

```bash
bash codeshare_send.sh test.txt https://codeshare.io/aVE8dR
```

Elle accepte aussi `--dry-run` en dernier argument. Les deux scripts utilisent le protocole GraphQL actuellement employé par Codeshare ; une évolution de ce protocole peut nécessiter leur mise à jour.
