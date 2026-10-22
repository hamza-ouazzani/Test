#!/usr/bin/env python3
"""Labellise une image SFM : complète les 8 labels manquants ou vides, garde ceux déjà remplis,
repousse sous le MÊME tag avec un contenu identique, puis propose de supprimer l'ancien digest.

  python3 relabel_image.py reg-ec002i001710/microseg-sref-app-dev:70666df9b6 [image_de_base]

Rien n'est modifié sans confirmation (o/N).
"""
import json
import re
import subprocess
import sys
from urllib.parse import quote

PUSH = "cr-admin-global.svc.paas.echonet:4222"  # registry pour docker pull/push
ICR = "fr2.icr.io"                              # nom des images côté API
API = "https://private.fr2.icr.io/api/v1"
BASE_REPOS = ("microseg-pybase", "microseg-gunicorn", "python", "py-image", "rhel9")  # où chercher la base
B, P = "bnpp.container.governance.image", "org.opencontainers.image"
LABELS = [f"{B}.auid", f"{B}.category", f"{P}.created", f"{P}.description",
          f"{P}.source", f"{P}.vendor", f"{P}.base.name", f"{P}.base.digest"]
DEFAUTS = {f"{B}.auid": "AP24624", f"{B}.category": "APPLICATION", f"{P}.vendor": "BNPP",
           f"{P}.source": "https://gitlab-dogen.group.echonet/Production-mutualisee/IPS/team_microsegmentation/programme_microsegmentation/microdev.git"}


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def api(path, method="GET"):
    out = sh(["curl", "-sS", "--noproxy", "*", "-X", method, "-w", "\n%{http_code}",
              "-H", f"Authorization: Bearer {token}", "-H", f"Account: {account}", API + path])
    body, code = out.rsplit("\n", 1)
    if not code.startswith("2"):
        sys.exit(f"API {method} {path} -> HTTP {code} {body[:200]}")
    return json.loads(body) if body.strip() else None


def dinspect(ref):
    return json.loads(sh(["docker", "inspect", ref]))[0]


def repo_of(ref):  # x/reg-y/repo:tag -> x/reg-y/repo (le ":4222" du registry n'est pas un tag)
    ref = ref.split("@")[0]
    return ref[:ref.rfind(":")] if ref.rfind(":") > ref.rfind("/") else ref


def digest_of(ref):
    return next(d.split("@")[1] for d in dinspect(ref)["RepoDigests"] if d.startswith(repo_of(ref)))


def chercher_base(repo_voulu=None):
    """Image du registry dont les couches sont le plus long début de celles de l'image."""
    best = (None, None, 0)
    for c in api(f"/images?includeIBM=false&namespace={ns}"):
        if not c.get("RepoTags") or not c.get("RepoDigests"):
            continue
        r = c["RepoTags"][0].split("/", 2)[2]  # fr2.icr.io/reg-x/repo:tag -> repo:tag
        if (repo_of(r) != repo_voulu) if repo_voulu else not r.startswith(BASE_REPOS):
            continue
        cl = (api(f"/images/{quote(c['RepoDigests'][0], safe='')}/json").get("RootFS") or {}).get("Layers") or []
        if best[2] < len(cl) < len(layers) and layers[:len(cl)] == cl:
            best = (f"{PUSH}/{ns}/{r}", c["RepoDigests"][0].split("@")[1], len(cl))
    return best[0], best[1]


arg = sys.argv[1]                      # reg-ec002i001710/repo:tag
ns, img = arg.split("/")[0], f"{PUSH}/{arg}"
token = [l for l in sh(["ibmcloud", "iam", "oauth-tokens"]).splitlines() if "IAM token" in l][0].split()[-1]
account = json.loads(sh(["ibmcloud", "target", "--output", "json"]))["account"]["guid"]

# 1. Image actuelle
sh(["docker", "pull", img])
ins = dinspect(img)
old, layers = digest_of(img), ins["RootFS"]["Layers"]
lb = ins["Config"].get("Labels") or {}
if all(lb.get(l) for l in LABELS):
    sys.exit("Image déjà labellisée (8 labels remplis) : rien à faire")

# 2. Image de base (seulement si base.name ou base.digest manque)
base_name, base_digest = lb.get(f"{P}.base.name"), lb.get(f"{P}.base.digest")
if len(sys.argv) > 2:
    base_name, base_digest = sys.argv[2], None
if not (base_name and base_digest):
    if base_name:  # on vérifie que la base indiquée correspond bien aux couches de l'image
        if subprocess.run(["docker", "pull", base_name], capture_output=True).returncode == 0:
            bl = dinspect(base_name)["RootFS"]["Layers"]
            if len(bl) <= len(layers) and layers[:len(bl)] == bl:  # <= : une image mirror a les mêmes couches que l'originale
                base_digest = digest_of(base_name)
        if not base_digest:  # le tag de base a bougé depuis le build : on cherche l'ancien digest dans le même repo
            _, base_digest = chercher_base(re.sub(r"^.*?reg-ec\w+/", "", repo_of(base_name)))
    else:
        base_name, base_digest = chercher_base()
if not base_digest:
    sys.exit("Image de base introuvable : relancer avec l'image de base en 2e argument")

# 3. Labels : on garde ceux déjà remplis, on complète les autres
nom = repo_of(arg).split("/")[-1].replace("microseg-", "").split("-app")[0]
calcules = dict(DEFAUTS, **{f"{P}.created": sh(["date", "-d", ins["Created"], "--rfc-3339=seconds"]),
                            f"{P}.description": f"{nom} image for SFM",
                            f"{P}.base.name": base_name, f"{P}.base.digest": base_digest})
labels = {l: lb.get(l) or calcules[l] for l in LABELS}

print(f"\nImage : {arg}\nDigest actuel : {old}\n")
for l in LABELS:
    print(f"  {'OK     ' if lb.get(l) else 'AJOUTÉ '} {l} = {labels[l]}")
if not repo_of(arg).split("/")[-1].startswith("microseg-"):
    print("\n  ATTENTION : image non microseg, vérifier vendor/category/source avant de valider")
if input("\nRebuild (contenu identique) et push sous le même tag ? [o/N] ").lower() != "o":
    sys.exit("Annulé, rien n'a été modifié")

# 4. Rebuild : FROM ancienne@digest + labels -> même contenu, même tag
cmd = ["docker", "build", "-t", img] + sum((["--label", f"{k}={v}"] for k, v in labels.items()), []) + ["-"]
sh(cmd, input=f"FROM {repo_of(img)}@{old}\n")
neuf = dinspect(img)
if neuf["RootFS"]["Layers"] != layers or not all((neuf["Config"]["Labels"] or {}).get(l) for l in LABELS):
    sys.exit("ERREUR : contenu modifié ou labels incomplets, push annulé")
sh(["docker", "push", img])
print(f"\nPush OK : contenu identique, 8 labels remplis\n  ancien digest : {old}\n  nouveau digest : {digest_of(img)}")

# 5. Suppression de l'ancien digest (désormais sans tag)
specs = sh(["kubectl", "get", "deploy,rs,sts,ds,cronjob,job", "-A", "-o", "json"])
if old in specs:
    print("\nAncien digest épinglé @sha256 dans un workload : mettre à jour le manifest AVANT de le supprimer")
elif input(f"\nSupprimer l'ancien digest {old} ? [o/N] ").lower() == "o":
    api("/images/" + quote(f"{ICR}/{repo_of(arg)}@{old}", safe=""), "DELETE")
    print("Ancien digest supprimé (récupérable ~30 j dans la corbeille IBM)")
subprocess.run(["docker", "rmi", img], capture_output=True)
