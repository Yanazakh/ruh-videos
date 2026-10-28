# -*- coding: utf-8 -*-
"""RELAIS DE PUBLICATION — tourne sur GitHub Actions, même quand le PC Shadow est éteint (28/10).

Demande utilisateur : « automatiser la diffusion même si Shadow n'est pas allumé » (option 2, gratuite).
Le Shadow fabrique les vidéos, les dépose en release de ce dépôt et les inscrit dans `file.json` ;
ce script, lancé toutes les 30 min par .github/workflows/publier.yml :
  1. remplit Buffer dans l'ordre des créneaux tant qu'il reste de la place (offre gratuite : 10 posts
     programmés par compte) — c'est Buffer qui publie, à l'heure exacte ;
  2. publie tout de suite une vidéo dont le créneau est arrivé sans qu'elle ait pu être programmée ;
  3. marque « manqué » une vidéo en retard de plus de 2 h (le Shadow lui redonnera un créneau).
État indexé par « nom@créneau ». Deux fichiers, deux écrivains : `file.json` n'est écrit que par le Shadow, `etat.json` que par ce script.
Bibliothèque standard seulement. Secret requis : BUFFER_API_KEY.
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

ICI = Path(__file__).resolve().parent
LIMITE = 10
CHAINES = {"instagram": "6a9ae8c1065799be4689f5af", "tiktok": "6a9ae333065799be4689db63",
           "facebook": "6ac7b10b6a5c39ccb6520fe7"}
MUTATION = """mutation CreatePost($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id status dueAt channelId } }
  ... on NotFoundError { message } ... on UnauthorizedError { message } ... on UnexpectedError { message }
  ... on RestProxyError { code message } ... on LimitReachedError { message } ... on InvalidInputError { message } } }"""


def gql(query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request("https://api.buffer.com", method="POST",
                                 data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Authorization": f"Bearer {os.environ['BUFFER_API_KEY'].strip()}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def programmes() -> dict:
    """Nombre de posts programmés par compte (l'API pagine)."""
    org = gql("{ account { organizations { id } } }")["data"]["account"]["organizations"][0]["id"]
    n, apres = {c: 0 for c in CHAINES.values()}, ""
    while True:
        d = gql('{ posts(first: 100%s, input: {organizationId: "%s"}) { pageInfo { hasNextPage endCursor } '
                'edges { node { status channelId } } } }' % (apres, org))["data"]["posts"]
        for e in d["edges"]:
            if e["node"]["status"] == "scheduled" and e["node"]["channelId"] in n:
                n[e["node"]["channelId"]] += 1
        if not d["pageInfo"]["hasNextPage"]:
            return n
        apres = ', after: "%s"' % d["pageInfo"]["endCursor"]


def envoyer(v: dict, quand: dt.datetime | None) -> tuple[dict, list]:
    ids, erreurs = {}, []
    for service, canal in CHAINES.items():
        meta = {"instagram": {"instagram": {"shouldShareToFeed": True, "type": "reel", "isAiGenerated": True}},
                "tiktok": {"tiktok": {"title": v["titre"], "isAiGenerated": True}},
                "facebook": {"facebook": {"type": v.get("facebook", "reel")}}}[service]
        inp = {"channelId": canal, "text": v["texte"], "schedulingType": "automatic", "needsApproval": False,
               "assets": [{"video": {"url": v["url"], "metadata": {"thumbnailOffset": v["miniature_ms"]}}}],
               "metadata": meta, "mode": "customScheduled" if quand else "shareNow"}
        if quand:
            inp["dueAt"] = quand.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        r = gql(MUTATION, {"input": inp})
        res = (r.get("data") or {}).get("createPost") or {}
        if "post" in res:
            ids[service] = res["post"]["id"]
        else:
            erreurs.append(f"{service} : {res.get('message') or json.dumps(r)[:200]}")
    return ids, erreurs


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    file = json.loads((ICI / "file.json").read_text(encoding="utf-8")) if (ICI / "file.json").exists() else []
    fe = ICI / "etat.json"
    etat = json.loads(fe.read_text(encoding="utf-8")) if fe.exists() else {}
    maint = dt.datetime.now(dt.timezone.utc)
    places = None
    for v in sorted(file, key=lambda v: v["quand"]):
        cle = f"{v['nom']}@{v['quand']}"                   # une vidéo replacée à un autre créneau repart de zéro
        e = etat.get(cle, {})
        if e.get("statut") in ("programmé", "publié", "manqué") or e.get("essais", 0) >= 3:
            continue
        quand = dt.datetime.fromisoformat(v["quand"].replace("Z", "+00:00"))
        reste = (quand - maint).total_seconds()
        if reste < -7200:
            etat[cle] = {"statut": "manqué", "le": maint.isoformat()}
            print(f"{v['nom']} : créneau manqué ({v['quand']})")
            continue
        if reste < 600:                                    # créneau arrivé : publication immédiate
            ids, err = envoyer(v, None)
            statut = "publié"
        else:
            if places is None:
                places = programmes()
            if max(places.values()) >= LIMITE:             # Buffer plein : on attend qu'un post parte
                print(f"Buffer plein ({places}) — {v['nom']} attend")
                break
            ids, err = envoyer(v, quand)
            statut = "programmé"
            for service in ids:
                places[CHAINES[service]] += 1
        if ids and not err:
            etat[cle] = {"statut": statut, "ids": ids, "le": maint.isoformat()}
            print(f"{v['nom']} : {statut} ({v['quand']})")
        elif ids:                                          # envoi partiel : on ne renvoie pas (doublons)
            etat[cle] = {"statut": statut, "ids": ids, "erreurs": err, "le": maint.isoformat()}
            print(f"{v['nom']} : {statut} partiel — {err}")
        else:
            etat[cle] = {"statut": "erreur", "erreurs": err, "essais": e.get("essais", 0) + 1,
                              "le": maint.isoformat()}
            print(f"{v['nom']} : erreur — {err}")
    fe.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
