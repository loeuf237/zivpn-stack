# Déploiement et migration

## Préparer la machine

Utiliser une nouvelle VM Ubuntu 24.04 ou Debian 12, x86-64 ou ARM64, avec systemd et IPv4 publique stable. Disposer d’un compte avec sudo, de place pour compiler et d’un accès sortant HTTPS vers GitHub, go.dev, PyPI et Telegram. Le téléchargement des modules Go utilise aussi le proxy et la base de sommes Go.

Les services utilisent les chemins Linux habituels ; aucun SDK ni identifiant cloud n’est requis. Les plafonds sont appliqués par le serveur Go, indépendamment des cartes réseau virtuelles des fournisseurs.

## Ouvrir les ports

Dans le pare-feu cloud **et** celui de la VM, autoriser UDP 5667. Autoriser UDP 5668 si nécessaire pour d’anciens profils Premium. Les alias UDP 5666, 80, 443, 8080, 8443, 8888 et 6000–30000 sont redirigés vers 5667 ; ouvrir seulement les alias réellement utilisés par les clients.

Azure utilise les règles entrantes du NSG ; Google Cloud les règles de pare-feu VPC ; AWS les règles du Security Group et, si personnalisé, du NACL ; Alibaba Cloud celles du Security Group. Aucun de ces objets n’est créé automatiquement.

Conserver SSH accessible à l’administrateur. Le panneau écoute exclusivement sur 127.0.0.1:2053 ; le bot fonctionne par réception longue et ne requiert aucun webhook entrant. Le relais natif crée ses propres connexions sortantes et n’a pas besoin d’une règle FORWARD acceptant tout.

## Préparer la configuration

Cloner le dépôt, puis préparer hors du dépôt deux fichiers accessibles uniquement à root :

```bash
sudo install -d -m 700 /root/zivpn-config
sudo install -m 600 config/settings.example.json /root/zivpn-config/settings.json
sudo install -m 600 config/secrets.example.json /root/zivpn-config/secrets.json
sudoedit /root/zivpn-config/settings.json /root/zivpn-config/secrets.json
```

Dans `settings.json`, saisir l’adresse publique ou le domaine et l’identifiant Telegram de l’administrateur principal. `network_interface: null` détecte l’interface IPv4 par défaut ; une interface peut être spécifiée pour les mesures du bot. Dans `secrets.json`, saisir le jeton du **nouveau bot** et un mot de passe unique de panneau d’au moins 20 caractères. Deux serveurs ne doivent pas effectuer simultanément `getUpdates` sur le même bot.

```bash
sudo python3 tools/install.py --settings /root/zivpn-config/settings.json --secrets /root/zivpn-config/secrets.json --check
sudo python3 tools/install.py --settings /root/zivpn-config/settings.json --secrets /root/zivpn-config/secrets.json
sudo systemctl is-active x-ui zivpn zivpn-xui-sync
```

L’installateur ne constitue pas une mise à jour idempotente : si une installation échoue après avoir créé des fichiers, diagnostiquer son état avant de reprendre. Il refuse toute installation existante, même incomplète, pour protéger ses données.

## Vérifier

Ouvrir un tunnel `ssh -L 2053:127.0.0.1:2053 utilisateur@serveur`, puis utiliser le panneau à l’adresse locale avec le chemin aléatoire enregistré dans le réglage `webBasePath`. Lire ce réglage localement :

```bash
sudo python3 -c "import sqlite3; c=sqlite3.connect('/etc/x-ui/x-ui.db'); print(c.execute(\"SELECT value FROM settings WHERE key='webBasePath'\").fetchone()[0])"
```

Dans la conversation privée de l’administrateur principal, utiliser `/menu`, `/status`, `/diagnostic`, `/annuler` et `/taches`. Créer deux comptes de vérification avec des mots de passe uniques :

```text
/add essai_standard mot-de-passe-unique standard 1
/add essai_premium autre-mot-de-passe premium 4
/qualite
/sante 24
```

La syntaxe est `/add <nom> <mot-de-passe> <standard|premium> [Mo/s]`. Sans vitesse, les valeurs initiales sont 1 Mo/s par IP publique pour Standard et 4 Mo/s par compte pour Premium. `/vitesse <nom> <Mo/s|defaut>` personnalise ou réinitialise le plafond ; `/profil <nom> <standard|premium> [Mo/s]` change le profil. `/limit standard <Mo/s>` et `/limit premium <Mo/s>` changent les valeurs par défaut, en conservant les plafonds personnalisés. Voir [la surveillance](OBSERVABILITY.md) pour les règles de partage et de reconnexion.

Configurer Android avec l’adresse du serveur et le mot de passe du compte. Le certificat est auto-signé pour la compatibilité existante : le client doit accepter ce mode. Vérifier navigation TCP et UDP, expiration, partage Standard par IP et partage Premium par compte avant de basculer des utilisateurs.

## Migration privée

La restauration des utilisateurs est distincte du déploiement du code. Faire une sauvegarde cohérente SQLite via son API de sauvegarde, en incluant les tables de plafonds `zivpn_qos_defaults` et `zivpn_qos_accounts` ; transférer la base et, si nécessaire, les certificats et secrets par un canal privé. Préserver les quotas et compteurs. Une migration de compteurs natifs nécessite un dernier échantillon avant l’arrêt du serveur source. Ne jamais restaurer une base pendant que le panneau ou le collecteur écrivent dedans. Cette restauration n’est pas automatisée par l’installateur neuf.

Après restauration, vérifier les profils, les valeurs par défaut et les plafonds personnalisés dans le bot. Sur une ancienne base sans ces tables, les comptes héritent initialement des valeurs 1 Mo/s et 4 Mo/s ; le bot initialise les tables. Une mise à jour de production suit les [contrôles de mise à jour](OBSERVABILITY.md#contrôles-de-mise-à-jour), jamais l’installateur neuf.
