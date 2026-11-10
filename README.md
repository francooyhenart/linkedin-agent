# Postline

Planificador local para mantener LinkedIn activo con 2 o 3 posts cortos por semana, en inglés y sin pinta de IA. Todo corre en tu compu: el texto lo escribe un modelo local con Ollama, los datos quedan en un archivo SQLite y el panel se abre en el navegador.

## Qué hace

- **Ideas**: anotás en castellano y desprolijo cosas concretas de tu semana. Son la materia prima.
- **Planificar próximos posts**: toma las ideas sin usar y arma borradores en inglés en tus próximos días de publicación.
- **Calendario**: ves y movés todo por fecha. Cada post pasa por *Borrador → Programada → Publicada*. Solo lo que marcás como *Programada* sale solo; nada se publica sin que lo hayas leído.
- **Editor**: contador de caracteres, aviso de frases que suenan a IA, botones de “Más corto”, “Más casual”, etc., y una vista previa que marca dónde LinkedIn corta con “…ver más”.
- **Métricas**: cargás impresiones, reacciones, comentarios y compartidos de cada post y ves totales, promedio y tasa de interacción. Arriba del calendario hay una tira con las últimas 8 semanas para ver si sostenés el ritmo.

## Instalación

Necesitás Python 3.10 o más nuevo y [Ollama](https://ollama.com).

```bash
ollama pull llama3.1:8b          # o el modelo que prefieras

cd postline
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
python app.py
```

Abrí http://localhost:8000. Con 8 GB de RAM anda un modelo de 7–8B; si tenés poca memoria probá uno de 3B.

## Conectar LinkedIn (opcional)

Sin conectar, la app funciona igual: a la hora programada el post queda en naranja como “Publicar a mano”, tocás “Copiar y abrir LinkedIn”, pegás y listo.

Para que publique solo usa la API oficial (no scraping, que es lo que hace que suspendan cuentas):

1. Creá una app en https://developer.linkedin.com (pide asociarla a una página de empresa; alcanza con una página simple).
2. En *Products* agregá **Sign In with LinkedIn using OpenID Connect** y **Share on LinkedIn**.
3. En *Auth* agregá la redirección `http://localhost:8000/auth/linkedin/callback`.
4. Pegá Client ID y Client Secret en `.env`, reiniciá y tocá **Conectar LinkedIn** en Ajustes.

LinkedIn da el permiso por 60 días; el panel avisa cuando falta poco para renovarlo.

## Cosas a saber

- **La app tiene que estar abierta** a la hora del post. Si la compu estuvo apagada, al prender publica lo atrasado hasta 12 horas (`MAX_LATE_HOURS`); lo más viejo queda como “Publicar a mano” para que no salgan tres posts juntos.
- **Métricas a mano**: LinkedIn no da estadísticas de perfiles personales por API.
- **Que suene a vos**: lo que más mejora el resultado es pegar 2 o 3 posts tuyos en Ajustes → “Posts tuyos de ejemplo”, y que las ideas tengan detalles reales (qué herramienta, cuánto tardaste, qué salió mal).

## Estructura

```
app.py              API, modelo local, publicación y programador (cada 1 minuto)
static/index.html   el panel completo
data.db             se crea solo; tus ideas, posts y métricas
```

## Próximo paso: postulaciones

La base ya deja lugar para sumar una sección de búsqueda laboral: registro de empresas a las que mandaste CV (fecha, puesto, estado, contacto, link), análisis de avisos contra tu CV con el mismo modelo local y adaptación del CV por puesto.
