# Módulo `jobs` (todavía vacío)

Esta carpeta solo reserva el lugar y tiene las mismas capas que `content`
(`router`, `schemas`, `service`, `repository`, `models`). No hay nada implementado.

Más adelante va a hacer:

- **Registro de postulaciones**: empresa, puesto, fecha, estado, contacto y link.
- **Análisis de ofertas contra el CV**: compara un aviso de trabajo con tu CV usando el cliente
  de `shared/llm`.
- **Recomendaciones de CV** por puesto, también con `shared/llm`.

Sus rutas van a colgar de `/api/jobs/...`. Igual que cualquier módulo, no va a importar
`models` ni `repository` de `content`: si necesita algo de ahí, le pide al service.
