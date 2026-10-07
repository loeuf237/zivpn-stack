# ZiVPN Stack

Serveur UDP compatible avec l’application Android ZiVPN, administration 3X-UI et bot Telegram. Le code du serveur natif, les outils et les tests sont versionnés ; les données des utilisateurs et les secrets restent sur chaque serveur.

## Fonctionnement

- Tous les comptes Android utilisent UDP **5667**. UDP 5668 sert à la compatibilité Premium.
- Standard : **1 000 000 octets/s par adresse IP publique par défaut**, partagés entre les tunnels et comptes de cette IP.
- Premium : **4 000 000 octets/s par compte par défaut**, partagés entre ses adresses et tunnels.
- Le profil est choisi explicitement à la création ; `/profil` le modifie et `/vitesse` personnalise le plafond. Si plusieurs comptes Standard partagent une IP, le plafond le plus bas des comptes connectés s’applique.
- Les plafonds combinent émission et réception. Les quotas et dates d’expiration viennent de 3X-UI.
- Les sessions et compteurs sont exposés uniquement via un socket Unix privé.
- Le bot propose rapports, gestion des comptes, diagnostics annulables et suivi `/taches`.
- Les observations de destinations et DNS durent au plus 30 minutes en mémoire. Elles ne déchiffrent pas HTTPS et ne prouvent pas l’utilisation d’une application.

## Gérer les comptes depuis Telegram

Les commandes suivantes sont réservées à l’administrateur principal dans sa conversation privée avec le bot. Remplacer les mots de passe fictifs par des valeurs uniques.

```text
/add alice mot-de-passe-unique standard 1
/add bob autre-mot-de-passe premium 4
/vitesse alice 0.75
/profil alice premium 6
/vitesse alice defaut
/limit standard 1
/limit premium 4
```

`/add <nom> <mot-de-passe> <standard|premium> [Mo/s]` impose un profil explicite ; sans vitesse, il utilise son plafond par défaut. `/vitesse` modifie le plafond personnalisé ou rétablit son héritage avec `defaut`. `/profil` conserve mot de passe, quotas, expiration et compteurs ; le compte concerné doit se reconnecter. Sans vitesse précisée, un plafond personnalisé existant est conservé. `/limit` modifie les valeurs par défaut sans écraser les plafonds personnalisés.

Les réglages persistent dans la base privée 3X-UI. Une valeur de **1 Mo/s correspond à 1 000 000 octets/s**. Le plafond représente un maximum partagé, pas une vitesse garantie.

## Organisation

`native/` contient le fork Go et ses dépendances déclarées ; `src/` le bot, les règles d’accès, la comptabilité et leurs tests ; `scripts/` les outils administratifs ; `deploy/systemd/` les services ; `config/` les modèles ; `tools/` la construction et l’installation.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PATH="$PWD/.venv/bin:$PATH" make test
make build
make test-native
```

La compilation télécharge un outil Go dont la version et le SHA-256 sont fixés dans `config/artifacts.json`. `build/` et `.cache/` ne sont pas versionnés. Les sources Go existantes requièrent actuellement Go 1.21.13 ; une migration vers une chaîne Go maintenue nécessite également la mise à niveau du fork QUIC.

## Installation sur un nouveau serveur

Voir [DEPLOYMENT.md](docs/DEPLOYMENT.md). L’installateur refuse d’écraser une installation existante. Il installe 3X-UI v3.8.5 avec vérification SHA-256, construit le serveur, crée les deux catégories de comptes et installe le bot. **Aucun compte VPN ni secret du serveur d’origine n’est cloné.**

Cibles prévues : Ubuntu 24.04 et Debian 12 avec Python ≥ 3.10, systemd, IPv4 et architecture x86-64 ou ARM64. Une VM répondant à ces critères peut être hébergée chez Azure, Google Cloud, AWS, Alibaba Cloud ou un autre fournisseur. La création des ressources et leurs règles réseau restent à configurer chez le fournisseur. La matrice cloud et ARM64 n’a pas encore été validée sur des VM neuves.

Ne lancez pas l’installateur neuf sur la production actuelle. Voir [SECURITY.md](SECURITY.md) pour les exclusions Git et les migrations privées, et [THIRD_PARTY.md](THIRD_PARTY.md) pour les licences.

## Surveillance et validation

`/sante 24` ou `/sante 48` affiche l’historique disponible ; `/qualite` mesure les débits partagés et les attentes du limiteur pendant cinq secondes. Ces rapports sont réservés à l’administrateur principal en privé. Voir [la surveillance](docs/OBSERVABILITY.md) pour leur interprétation et [l’état de validation](docs/VALIDATION.md) pour les tests et les limites du déploiement portable.
