# Reglas de trabajo para este proyecto

Estas reglas las puso el dueño del proyecto y valen para todo el trabajo, en todas las fases.

## 1. "Mostrame antes" significa parar

Cuando el pedido dice "mostrame antes", "explicame antes" o algo equivalente, NO ejecutes ni apliques
nada hasta que el usuario responda. Mostrar el contenido en el mismo turno en que lo aplicás no cuenta
como mostrarlo antes. Si hay duda sobre si hay que parar, parar y preguntar.

Esto vale en especial para: migraciones de base de datos, cambios de configuración, comandos que
modifican datos y cualquier cosa que sea difícil de deshacer.

## 2. Editar archivos con la herramienta de edición

Para modificar archivos existentes usá la herramienta Edit (o Write si se reescribe entero).
NO uses scripts de Python/sed/awk que hacen `replace` sobre el texto del archivo: si el texto buscado no
coincide, fallan en silencio y el cambio no se aplica sin que nadie se entere.

## 3. No tocar el .env del usuario

El archivo `.env` es del usuario: no lo modifiques ni lo sobrescribas (ni con scripts ni con sed).
Si hace falta una variable nueva o cambiar un valor:
1. Agregala o cambiala en `.env.example`.
2. Avisale al usuario, explícitamente, qué líneas tiene que copiar a su `.env`.

## 4. Windows: acentos y consola

En Windows los acentos rompen `grep` y la salida de la consola (cp1252 vs UTF-8), y la salida
se ve cortada o con líneas faltantes sin ningún error.
- No filtres con `grep`, `head`, `sed` la salida que se le muestra al usuario para que revise algo
  (SQL, migraciones, diffs, logs). Mostrala completa.
- Si hay que mostrar algo con acentos, forzá UTF-8 (`PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`),
  redirigí a un archivo y leelo con la herramienta Read, que sí muestra UTF-8 bien.
- Y nunca des por bueno un resultado que se ve raro o incompleto: verificalo por otra vía.
