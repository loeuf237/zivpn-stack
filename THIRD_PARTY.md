# Composants tiers

`native/` dérive de Hysteria, commit `405572dc6e335c29ab28011bcfa9e0db2c45a4b4` (licence MIT conservée dans `native/LICENSE.md`). Les modifications ajoutent le protocole Android ZiVPN, les plafonds natifs, l’authentification 3X-UI et l’observation des destinations/DNS. Les dépendances sont répertoriées dans les fichiers `go.mod` et `go.sum`.

3X-UI est téléchargé séparément depuis MHSanaei/3x-ui, version v3.8.5, sous licence GPL-3.0 ; son binaire et son code ne sont pas copiés dans ce dépôt. Voir https://github.com/MHSanaei/3x-ui/tree/v3.8.5.

La constante de protocole d’obfuscation Android dans le code Go est une constante partagée de compatibilité, pas un secret de compte ni une protection d’authentification.
