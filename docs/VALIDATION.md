# État de validation

Le code de ce dépôt est issu des corrections déployées sur le serveur source : plafonds natifs par IP/compte, compatibilité Android, comptabilité transactionnelle, gestion des comptes, rapports Telegram et tâches asynchrones. Le dépôt remplace les paramètres propres au serveur par une configuration cible ; ces adaptations ne modifient pas la production existante.

La suite Python précédente contient 76 tests. Les tests de configuration et d’archives ajoutent cinq vérifications. La compilation Go est effectuée à partir des sources du dépôt. Les essais QoS utilisent de vrais tunnels TCP/UDP en boucle locale ; ils ne valident pas les routes ni pare-feu d’un cloud. Les anciens tests de stress transférant plusieurs gigaoctets sont exclus de la validation normale (`-skip Stress`) et doivent être exécutés séparément sur une machine isolée.

L’initialisation du panneau est vérifiée sur une base temporaire créée par le binaire 3X-UI v3.8.5, sans accès à la base de production. L’installateur entier et les règles réseau doivent encore être validés sur une VM jetable neuve avant migration. Aucun test Telegram automatisé n’envoie un message aux administrateurs.

Les études et snapshots de maintenance spécifiques à l’ancien serveur restent dans le workspace privé ; seuls leurs résultats applicables sont repris ici. Les anciens scripts d’authentification, de shaping noyau et les déploiements ponctuels ont été remplacés par l’état actuel, pour éviter de réintroduire les erreurs corrigées.

Les tests de fermeture UDP ont révélé une course sur le marqueur de timeout du code de base. Le dépôt utilise désormais un booléen atomique ; cette correction et la configuration portable ne sont pas encore déployées sur le serveur source. Les certificats des tests de tunnel sont générés en mémoire et ne sont pas versionnés.
