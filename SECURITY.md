# Secrets et données d’exploitation

Ce dépôt doit contenir uniquement sources, tests avec données fictives, modèles et documentation. Ne jamais y ajouter jetons Telegram/GitHub, mots de passe réels, `clients.csv`, bases SQLite, clés privées, sauvegardes, captures de connexions ni journaux de trafic.

`.gitignore` exclut ces fichiers usuels. `tools/secret_scan.py --staged` inspecte les blobs de l’index avant un commit ; le contrôle initial avec `--live` compare aussi les fichiers aux secrets du serveur courant sans afficher leurs valeurs. Ce contrôle ne remplace pas la revue des fichiers.

Les secrets cibles résident sous `/etc/zivpn` et dans `/etc/x-ui/x-ui.db`, avec accès root. Le bot dispose de commandes administratives puissantes : `/exec` et les rapports de destinations/DNS restent réservés à l’administrateur principal dans sa conversation privée. Conserver son compte Telegram protégé.

Le jeton se renouvelle par `sudo /usr/local/sbin/zivpn-set-telegram-token` avec saisie masquée. Les anciens jetons sauvegardés restent privés. Ne jamais passer un jeton ou un mot de passe dans une commande Git, un message d’issue ou la description d’un PR.

Les données de trafic, statistiques utilisateur et rapports de sécurité de production ne sont pas publiés. L’observateur natif n’enregistre pas le contenu chiffré ; la classification d’applications reste indicative.

GitHub CLI conserve ici son authentification dans le fichier utilisateur `~/.config/gh/hosts.yml`, protégé en 0600 et exclu du dépôt. Le dépôt est créé privé par défaut. Pour une migration, suivre `docs/DEPLOYMENT.md` et transférer les données d’exploitation séparément.
