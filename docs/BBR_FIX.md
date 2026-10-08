# Corrections de congestion et diagnostic des pertes QUIC

## BBR : cadence et agrégation des ACK

Le contrôleur local contient les deux défauts corrigés par Hysteria dans
[la PR #1495](https://github.com/apernet/hysteria/pull/1495), intégrée à
[app/v2.7.0](https://github.com/apernet/hysteria/releases/tag/app%2Fv2.7.0).

- `bandwidthForPacer` utilisait le gain de fenêtre de congestion au lieu de
  `PacingRate()`. La fenêtre réserve des octets en vol ; son gain ne doit pas
  devenir le débit d'émission, notamment pendant le drainage.
- Le filtre d'agrégation des ACK enregistrait `expectedBytesAcked` au lieu de
  `extraBytesAcked`, faussant la marge calculée pour la fenêtre.

`pacing_regression_test.go` vérifie les gains de cadence 0,75, 1 et 1,25 et
l'excès d'octets acquittés. Ces régressions échouent avec les calculs précédents.
Le plancher de cadence existant de 65 536 octets/s est conservé.

## Choix explicite BBR, Reno ou CUBIC

Le serveur natif accepte `congestionControl` : `bbr`, `reno` ou `cubic`.
La valeur absente conserve BBR. Les valeurs inconnues sont refusées.
En mode Hysteria classique, un débit client explicite conserve le contrôleur
Brutal ; le serveur natif ignore ces demandes comme auparavant.

CUBIC était déjà présent dans la dépendance QUIC épinglée
`v0.40.1-0.20231112225043-e7f3af208dee`, au commit
[`e7f3af208dee0f09f2a187db908e1072875fdded`](https://github.com/HyNetworks/quic-go/commit/e7f3af208dee0f09f2a187db908e1072875fdded).
Le nom `NewCubicSender` peut induire en erreur : le fork passait `true` au
paramètre `reno`, ce qui sélectionne Reno. L'option locale `quic.Config.UseCubic`
passe explicitement `false` à ce paramètre pour activer CUBIC.
Le défaut du fork QUIC reste Reno ; le serveur applique son choix à part.
Les plafonds de charge utile Standard par IP et Premium par compte continuent
à s'appliquer indépendamment du contrôleur de congestion.

## Taille des paquets et enveloppe Salamander

`quic.initialPacketSize` définit la taille initiale de charge QUIC, hors
surcoût de l'obfuscation. Zéro conserve les défauts existants : 1 252 octets
sur IPv4, 1 232 sur IPv6 et 1 200 pour une adresse non UDP.
Les valeurs explicites doivent être comprises entre 1 200 et 1 452 octets.
`disablePathMTUDiscovery: true` maintient une taille fixe.

Extrait à ajouter à une configuration native complète :

```json
{
  "congestionControl": "cubic",
  "quic": {
    "initialPacketSize": 1200,
    "disablePathMTUDiscovery": true
  }
}
```

Salamander ajoute 8 octets : QUIC 1 200 produit donc une charge UDP de 1 208.
Un chemin IPv4 à MTU 1 280 admet seulement 1 252 octets de charge UDP.
`TestSalamanderConstrainedMTUDownload` admet l'authentification, puis impose
cette limite au transfert : le réglage IPv4 par défaut provoque des rejets
et une expiration de lecture ; QUIC 1 200 transfère les 131 072 octets attendus.
Ce test démontre le comportement sur un chemin simulé, sans établir la MTU
d'un réseau mobile réel. Une taille fixe désactive aussi les gains possibles
de découverte MTU sur les chemins moins contraints.

## Course de dérivation de clé Salamander

`append(o.PSK, salt...)` pouvait écrire dans le tableau partagé de la clé
lorsque sa capacité excédait sa longueur. Lecteurs et émetteurs concurrents
pouvaient alors produire des clés différentes et corrompre des paquets.
La dérivation utilise désormais un tampon privé contenant la même concaténation.
Le format réseau et le hachage restent identiques.

`concurrency_test.go` vérifie les clés avec et sans capacité supplémentaire,
avec plusieurs lecteurs et émetteurs et la détection de courses.
Une clé sans capacité supplémentaire ne reproduit pas ce défaut ; cette
correction ne suffit pas à attribuer toutes les pertes réseau à Salamander.

## Télémétrie des pertes et acquittements tardifs

La vue du transport ajoute les compteurs `lost_time_threshold`,
`lost_reordering_threshold`, `late_acked_packets` et `loss_tracking_evicted`,
ainsi que `congestion_window_bytes` et `in_flight_bytes`.
Aucun contenu de paquet ni clé n'est conservé.

La boucle de connexion suit au plus 4 096 numéros de paquets 1-RTT.
Un ACK ultérieur peut prouver qu'un paquet déclaré perdu avait été livré.
Il est compté une seule fois, sans modifier le mécanisme de récupération QUIC.
Les lecteurs du rapport accèdent aux compteurs atomiques, pas au tableau interne.

Le nombre d'acquittements tardifs est une borne inférieure : une entrée peut
être évincée ou sortir de la fenêtre avant l'ACK. Les pertes déclarées QUIC
ne mesurent donc pas exactement les pertes physiques. Soustraire les ACK
tardifs ne produit pas une mesure exacte du réseau.
`telemetry_loss_test.go` couvre doublons, niveaux de chiffrement, évictions
et plages d'ACK très larges.

## Simulations de liaison

Les tests d'intégration utilisent des identifiants fictifs, des certificats
générés en mémoire et des ports locaux. Le simulateur impose une capacité,
une file finie et 100 ms de propagation ; il n'ajoute aucune perte aléatoire.

Dans une exécution de `TestBBRFiniteQueueDownload` à 250 000 octets/s avec
32 datagrammes en file, le transfert complet de 3 342 336 octets donnait :

| Mesure synthétique | Calculs précédents | Calculs corrigés |
|---|---:|---:|
| Taux de rejet par la file | 35,19 % | 1,25 % |
| Débit utile | 214 725 octets/s | 208 088 octets/s |

La correction réduit le dépassement de file dans ce scénario ; elle ne promet
pas une hausse universelle du débit. Les essais BBR, Reno et CUBIC couvrent
également une liaison de 40 000 octets/s avec une file de huit datagrammes.
Les résultats dépendent de l'ordonnancement et les rejets ne disparaissent pas.

## Validation et limites

Les 92 tests Python et les contrôles de syntaxe sont exécutés avec
`make test` et `make check`. Pour les tests Go avec détection de courses :

```bash
cd native/core
GOTOOLCHAIN=local ../../.cache/go1.21.13-amd64/go/bin/go test -p 1 -race -skip Stress ./qos ./server ./cmd/zivpn-native ./internal/congestion/... ./internal/integration_tests ../quic ../extras/obfs
```

Les essais ciblés BBR, CUBIC, MTU, télémétrie et concurrence Salamander passent
avec `-race`. Le test QoS existant `TestTunneledSharedCeilingsAndMixedIP`
présente des échecs intermittents sur son minimum de débit Premium dans un
environnement chargé, y compris après retrait temporaire de l'option MTU.
Les essais n'ont pas constaté de dépassement des plafonds. Une réussite isolée
ne garantit pas que l'ensemble de la suite Go passe systématiquement.

Les tests ne redémarrent pas les services et n'envoient aucun message Telegram.
Pour une mise à jour, suivre les [contrôles de mise à jour](OBSERVABILITY.md#contrôles-de-mise-à-jour).
Les journaux et mesures de production doivent rester privés.
