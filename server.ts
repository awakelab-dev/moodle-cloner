import 'dotenv/config';
import express from 'express';
import mongoose from 'mongoose';
import cors from 'cors';
import * as path from 'path';
import { downloadSepeFile } from './descargar_sepe';
import { processLatestExcel } from './excelProcessor';
import { EspecificacionFormativa } from './models/EspecificacionFormativa';
import { Run } from './models/Run';
import { RunTracker } from './utils/runTracker';

const app = express();
const PORT = process.env.PORT || 3000;
const MONGODB_URI = process.env.MONGODB_URI || 'mongodb://localhost:27017/sepeDB';

// Middlewares
app.use(cors());
app.use(express.json());
// Servir archivos estáticos del frontend
// Servir archivos estáticos del frontend desde la raíz del proyecto
app.use(express.static(path.join(process.cwd(), 'public')));

// Ruta para obtener los registros guardados en MongoDB
app.get('/api/registros', async (req, res) => {
    try {
        // Obtenemos los últimos 50 registros, excluyendo campos internos de mongo
        const registros = await EspecificacionFormativa.find({}, { _id: 0, __v: 0, createdAt: 0, updatedAt: 0 }).limit(50).lean();
        res.json({ success: true, data: registros });
    } catch (error) {
        console.error('Error al obtener registros:', error);
        res.status(500).json({ success: false, message: 'Error interno al obtener datos de MongoDB.' });
    }
});

// Ruta para obtener el historial de corridas
app.get('/api/runs', async (req, res) => {
    try {
        const runs = await Run.find().sort({ createdAt: -1 }).limit(20).lean();
        res.json({ success: true, data: runs });
    } catch (error) {
        console.error('Error al obtener el historial de runs:', error);
        res.status(500).json({ success: false, message: 'Error al obtener datos de MongoDB.' });
    }
});

// Ruta principal para iniciar la automatización
app.get('/api/procesar-sepe', async (req, res) => {
    const tracker = new RunTracker();
    try {
        await tracker.start();
        await tracker.logStep('API_START', 'Iniciando proceso de descarga del SEPE...');

        // 1. Ejecutar Playwright y descargar el Excel
        const downloadedFilePath = await downloadSepeFile(tracker);

        if (!downloadedFilePath) {
            await tracker.logStep('DOWNLOAD_FAIL', 'Fallo al descargar el archivo con Playwright.');
            await tracker.finishError(new Error('Fallo al descargar el archivo con Playwright.'));
            return res.status(500).json({ success: false, message: 'Fallo al descargar.' });
        }

        await tracker.logStep('DOWNLOAD_SUCCESS', 'Archivo descargado. Iniciando el procesamiento a MongoDB...');

        // 2. Procesar el Excel (Parsearlo y subir a Mongo)
        // Usar la carpeta de descargas en la raíz del proyecto
        const downloadDir = path.resolve(process.cwd(), 'descargas');
        const first5Records = await processLatestExcel(downloadDir, tracker);

        // 3. Responder al frontend con los 5 registros
        res.json({
            success: true,
            data: first5Records,
            message: 'Proceso completado exitosamente.',
            runId: tracker.runId
        });

    } catch (error: any) {
        console.error('Error en el endpoint /api/procesar-sepe:', error);
        await tracker.finishError(error);
        res.status(500).json({ success: false, message: 'Error interno del servidor.', runId: tracker.runId });
    }
});

// Función para conectar a la DB y levantar el servidor
export const startServer = async () => {
    try {
        await mongoose.connect(MONGODB_URI);
        console.log(`Conectado a MongoDB en ${MONGODB_URI}`);

        return app.listen(PORT, () => {
            console.log(`Servidor corriendo en http://localhost:${PORT}`);
        });
    } catch (error) {
        console.error('Error al iniciar el servidor:', error);
        process.exit(1);
    }
};

if (process.env.NODE_ENV !== 'test') {
    startServer();
}

export { app };
