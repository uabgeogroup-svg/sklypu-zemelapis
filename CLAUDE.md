# Sklypų darbo žemėlapis – taisyklės Claude

## PRIVALOMA: žurnalo tiltas
`index.html` gale (po eilutės `window.GG={map,S,SET,...};`, prieš `}`, uždarantį `GG_APP`) yra blokas
„GeoGroup darbų žurnalo tiltas“ (prasideda `if(window.parent===window)return;`, siunčia `{src:'gg-map', type:'ready'}`).

- Jo NETRINTI ir NEKEISTI, atnaujinant ar perrašant žemėlapį (ir kuriant naują versiją iš kito failo).
- Per jį GeoGroup darbų žurnalas (repo `uabgeogroup-svg/geogroup-zurnalas`, `js/features/map.js`) atidaro šį žemėlapį pagal nuorodą
  ir gauna darbų taškus bei parinktą adresą.
- Be jo žurnalas po 8 s persijungia į seną vietinę kopiją (v11.5) – vartotojas mato seną žemėlapį.
- Prieš kiekvieną `git push` patikrink: `grep -c "src:'gg-map'" index.html` turi būti ≥ 1.

## Versija
Keičiant versiją atnaujinti ir `index.html` (v11.x), ir `version.json`.
