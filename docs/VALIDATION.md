# État de validation

Le code de ce dépôt est issu des corrections déployées sur le serveur source : plafonds natifs par IP/compte, compatibilité Android, comptabilité transactionnelle, gestion des comptes, rapports Telegram et tâches asynchrones. Le dépôt remplace les paramètres propres au serveur par une configuration cible ; ces adaptations ne modifient pas la production existante.

La suite Python contient 87 tests applicatifs et cinq tests de configuration et d’archives, soit 92 vérifications. Elle couvre notamment la création avec profil explicite, les plafonds persistants, leur héritage et la conservation des données lors d’un changement de profil. La compilation Go est effectuée à partir des sources du dépôt. Les essais QoS utilisent de vrais tunnels TCP/UDP en boucle locale ; ils ne valident pas les routes ni pare-feu d’un cloud. Les anciens tests de stress transférant plusieurs gigaoctets sont exclus de la validation normale (`-skip Stress`) et doivent être exécutés séparément sur une machine isolée.

L’initialisation du panneau est vérifiée sur une base temporaire créée par le binaire 3X-UI v3.8.5, sans accès à la base de production. L’installateur entier et les règles réseau doivent encore être validés sur une VM jetable neuve avant migration. Aucun test Telegram automatisé n’envoie un message aux administrateurs.

Les études et snapshots de maintenance spécifiques à l’ancien serveur restent dans le workspace privé ; seuls leurs résultats applicables sont repris ici. Les anciens scripts d’authentification, de shaping noyau et les déploiements ponctuels ont été remplacés par l’état actuel, pour éviter de réintroduire les erreurs corrigées.

Les tests de fermeture UDP ont révélé plusieurs courses dans le code de base. Le marqueur de timeout est atomique ; la fermeture du socket est exécutée une seule fois, même avec plusieurs nettoyages concurrents. Une session est enregistrée avant le lancement de sa boucle de réception, afin qu’un échec immédiat ne laisse pas de session fermée dans le registre. Des tests de régression couvrent ces cas. La configuration portable reste distincte de la configuration du serveur source. Les certificats des tests de tunnel sont générés en mémoire et ne sont pas versionnés.

## Stabilité QUIC

Le serveur natif envoie désormais un maintien QUIC toutes les cinq secondes, avec un délai maximal d’inactivité côté serveur de 60 secondes. Le délai effectif reste négocié avec le client ; la cadence de maintien est adaptée par la bibliothèque. Le test `TestServerKeepsIdleClientConnected` reproduit la fermeture d’un client sans maintien puis démontre la survie de la même connexion lorsque le serveur le fournit. Ces paquets de contrôle ne modifient pas les plafonds de charge utile Standard/Premium. Une interruption réelle du réseau reste susceptible de couper un tunnel.

Les fermetures ordinaires sont conservées dans la télémétrie privée et les rapports `/sante`, sans journaliser répétitivement chaque événement. Les motifs et durées permettent de distinguer inactivité et incidents réseau ; aucun mot de passe ou contenu n’y est ajouté. Les tests ne démontrent pas la correction des plaintes Android sur tous les opérateurs ; celle-ci nécessite une vérification client après déploiement.

## Contrôle de congestion concurrent

GitHub Actions a révélé une autre course, intermittente, entre les ACK QUIC et la sélection de BBR à l’authentification. Le fork local documenté dans `native/quic/ZIVPN-PATCHES.md` applique désormais ces changements dans la boucle de connexion. Le test ciblé de handshake et celui des setters concurrents passent sur 50 répétitions avec détection de courses. Il ne s’agit pas d’un changement des plafonds ou de l’algorithme BBR.

## Dernière validation du code

Le commit `7abd1a5` a passé les suites Go avec détection de courses et la [validation GitHub Actions](https://github.com/loeuf237/zivpn-stack/actions/runs/37564180721). Les essais locaux de vrais tunnels TCP/UDP vérifient les plafonds par défaut et personnalisés, ainsi que leur modification en fonctionnement. Les contrôles après déploiement ont confirmé les services actifs, les valeurs 1 Mo/s et 4 Mo/s et la conservation des comptes et compteurs. Ils ne constituent pas une mesure de débit garanti sur les réseaux mobiles.
