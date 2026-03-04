# Testing Guide

Este proyecto implementa una suite de automatización de QA con la finalidad de validar integraciones (vía Mongoose), flujos unitarios (parsing logico) y rutas de Express.

## Requisitos previos

La arquitectura se vale de:
- **Jest** & **ts-jest**: Test runner
- **mongodb-memory-server**: Servidor de base de datos intermitente que levanta una instancia local en memoria, aislada, logrando evitar mutaciones a la basae de datos o fallos de concurrencia.
- **Supertest**: Assertions de HTTP.

## Ejecución

El script `test` está preparado en `package.json` para ejecutar todas las integraciones mediante los siguientes módulos de CLI:

```bash
# Correr todo el banco de pruebas asincrónicamente
npm run test

# Correr por separado (recomendado si existen debugs precisos)
npm run test:unit
npm run test:integration
npm run test:api
```

### Controles de Calidad (Lint & Typecheck)
Para evitar fallos o caídas en runtime de node, el CI requiere:

```bash
npm run typecheck   # Valida tipados fuertes sin interactuar en emit.
npm run lint        # Valida estándares con ESLint
```

## Troubleshooting
Si obtienes fallos por parte de `MongoMemoryServer` (error de binarios download fallback o EACCESS):
- Intenta correr el comando como administrador la primera vez (para que MongoDB descargue el motor requerido en tu caché).
- Si usas proxies en tu equipo verifica el puerto de salida a descargas.
