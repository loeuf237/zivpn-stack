# Santé des connexions et historique d’exploitation

## Rapports privés

Le socket Unix natif, accessible uniquement à root, expose `/health` et `/snapshot`. Aucun accès HTTP public n’est ajouté. Les mesures QUIC incluent RTT lissé, paquets envoyés, acquittés ou déclarés perdus et nombre actuel de délais PTO consécutifs. L’activité utile conserve uniquement horodatages et compteurs d’octets : aucun contenu, mot de passe ou nom DNS dans cet historique.

`/sante [24|48]`, `/qualite` et leurs boutons sont réservés à l’administrateur principal dans sa conversation privée. `/sante` indique la couverture enregistrée, les motifs de fermeture, les tunnels courts, l’activité avant expiration, les résultats d’authentification et les erreurs Telegram. `/qualite` échantillonne pendant cinq secondes les débits des groupes et la fréquence des attentes du limiteur. Il affiche les plafonds effectifs.

## Conservation et alertes

Le processus natif conserve au maximum 8 192 fermetures pendant 48 heures. Le bot échantillonne chaque minute dans `/var/lib/zivpn-telegram/health.db`, protégé en 0600, avec une conservation de sept jours ou 100 000 fermetures. Les événements manquants sont comptés explicitement. Les comparaisons traversant un redémarrage natif ou une recréation de groupe sont écartées ; les erreurs d’authentification correspondent aux différences entre échantillons enregistrés. L’historique ne reconstitue pas les jours précédant sa collecte. Les erreurs du noyau décrivent la VM, pas le réseau mobile.

Les alertes sont regroupées, au plus une toutes les 30 minutes, uniquement pour l’administrateur principal : télémétrie ou services indisponibles, au moins trois erreurs internes d’authentification entre échantillons, augmentation des erreurs UDP/carte réseau, conntrack à 80 %, fermetures manquantes ou livraison Telegram incertaine. Le délai d’alerte persiste après redémarrage du bot. Les expirations d’inactivité seules ne déclenchent pas d’alerte de panne.

Un message Telegram dont la livraison est incertaine n’est pas renvoyé aveuglément. Les journaux emploient des catégories d’erreurs fixes, sans descriptions Telegram brutes. Authentifications réussies et fermetures ordinaires sont comptées ; les journaux de trafic sont regroupés toutes les dix minutes.

## Profils et plafonds personnalisables

Commandes de l’administrateur principal, en privé, avec mots de passe fictifs à remplacer :

- `/add alice mot-de-passe-unique standard 1` : Standard, 1 Mo/s par IP publique.
- `/add bob autre-mot-de-passe premium 4` : Premium, 4 Mo/s par compte, toutes IP confondues.
- `/vitesse alice 0.75` : plafond personnalisé ; `/vitesse alice defaut` rétablit l’héritage.
- `/profil alice premium 6` : change le profil et éventuellement la vitesse, en conservant compteurs, quotas, expiration et mot de passe. Le compte concerné doit se reconnecter. Sans vitesse, un plafond personnalisé existant est conservé ; sinon le nouveau défaut s’applique.
- `/limit standard 1` et `/limit premium 4` : changent les valeurs par défaut sans écraser les plafonds personnalisés.

Le profil est explicite, jamais déduit de la vitesse ou du port. Sans vitesse dans `/add`, le compte hérite de son défaut. Les valeurs initiales sont 1 Mo/s pour Standard et 4 Mo/s pour Premium. Un Mo/s vaut 1 000 000 octets/s. Les valeurs positives, d’un octet/s à 1 000 000 Mo/s, sont acceptées. Les tables `zivpn_qos_defaults` et `zivpn_qos_accounts` dans la base privée 3X-UI conservent ces réglages. Les comptes sans personnalisation héritent du défaut. Les noms ou mots de passe déjà utilisés sont refusés à la création.

Tous les comptes Standard d’une même IP publique partagent un seul groupe ; le plus petit plafond des comptes Standard connectés s’applique. Le départ d’un compte libère sa restriction. Un opérateur mobile peut regrouper plusieurs utilisateurs derrière une IP. Premium possède un groupe par compte, indépendant des Standard de la même IP. Dans les deux cas, émission et réception TCP/UDP partagent le plafond. Celui-ci est un maximum, pas une vitesse garantie ; des rafales peuvent le dépasser sur un intervalle court.

Les changements de vitesse modifient les limiteurs existants sans réinitialiser leur crédit de rafale. Les réservations de blocs en cours peuvent brièvement conserver leur ancienne planification. Un changement de profil révoque les tunnels de l’ancien profil. Les politiques sont transmises par le socket privé et revérifiées toutes les 15 secondes, même sans utilisateur connecté.

## Diagnostiquer Android

Comparer cinq minutes de navigation active, le passage en arrière-plan ou écran verrouillé et le Wi-Fi avec les données mobiles. Noter opérateur, heure et interruption réelle de navigation. Comparer RTT/débits de `/qualite` et fermetures/activité de `/sante`, sans copier les mots de passe dans les rapports. Modifier MTU ou maintien de connexion après cette comparaison contrôlée ; les seules mesures du serveur ne déterminent pas la cause côté téléphone.

## Contrôles de mise à jour

Sauvegarder les binaires, scripts, réglages de journalisation systemd et SQLite via son API de sauvegarde. Préserver les paramètres propres au serveur, l’administrateur principal et les alias historiques. Installer ensemble le helper structuré et le serveur natif : l’ancien protocole du helper est incompatible avec le nouvel authentificateur.

Exécuter `make test`, `make test-native`, les contrôles de syntaxe et le scan de secrets. Enregistrer les compteurs avant l’arrêt du VPN, puis redémarrer pour activer le binaire. Vérifier services, permissions du socket, plafonds attendus et compteurs monotones. En cas d’échec, restaurer ensemble helper, politique et serveur ; restaurer une ancienne base ferait perdre les données accumulées depuis. Ne jamais lancer l’installateur neuf sur une installation existante.
