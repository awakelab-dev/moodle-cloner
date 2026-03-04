import * as fs from 'fs';
import * as path from 'path';
import * as xlsx from 'xlsx';
import { EspecificacionFormativa, Modalidad, IOcupacion } from './models/EspecificacionFormativa';
import { RunTracker } from './utils/runTracker';

// --- Funciones auxiliares de transformación ---

/** Elimina acentos de una cadena de texto. */
export const removeAccents = (str: string): string => {
    return str.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
};

/** Limpia los espacios extremos del dato. */
export const formatDataValue = (str: string): string => {
    return str.trim();
};

/** Normaliza texto para valores legibles: conserva la capitalización original, acentos, reduce espacios múltiples y recorta extremos. */
export const normalizeText = (str: string): string => {
    return str.replace(/\s+/g, ' ').trim();
};

/** Convierte un string a número. Devuelve 0 si no es válido. */
export const parseNumber = (val: any): number => {
    const n = Number(String(val).trim());
    return isNaN(n) ? 0 : n;
};

/** Convierte un string a número o null. Devuelve null si el valor es "-", vacío o no numérico. */
export const parseNumberOrNull = (val: any): number | null => {
    const s = String(val).trim();
    if (s === '-' || s === '') return null;
    const n = Number(s);
    return isNaN(n) ? null : n;
};

/** Convierte "-" o vacío a null, de lo contrario devuelve el texto normalizado (sin acentos, minúscula, con espacios). */
export const parseStringOrNull = (val: any): string | null => {
    const s = String(val).trim();
    return (s === '-' || s === '') ? null : normalizeText(s);
};

/** Mapea el valor de MODALIDAD DE IMPARTICIÓN del Excel al enum Modalidad.
 * - Si contiene "Presencial" Y "Teleformación" → "mixta"
 * - Si contiene solo "Presencial" → "presencial"
 * - Si contiene solo "Teleformación" → "teleformacion"
 * - Si contiene "Mixta" → "mixta"
 */
export const mapModalidad = (val: string): Modalidad => {
    const clean = removeAccents(val).toLowerCase().trim();
    const tienePresencial = clean.includes('presencial');
    const tieneTeleformacion = clean.includes('teleformacion');

    // Si viene "mixta" directamente, o si trae ambas modalidades
    if (clean.includes('mixta') || (tienePresencial && tieneTeleformacion)) return 'mixta';
    if (tieneTeleformacion) return 'teleformacion';
    if (tienePresencial) return 'presencial';
    return 'presencial';
};

/**
 * Parsea la cadena de OCUPACIONES RELACIONADAS a un array de objetos { codigo, descripcion }.
 * Formato esperado: "31311142 - TÉCNICOS DE..., 29341012 - AYUDANTES DE..."
 * Si el valor es "-" o vacío, devuelve un array vacío.
 */
export const parseOcupaciones = (val: string): IOcupacion[] => {
    const s = String(val).trim();
    if (s === '-' || s === '') return [];

    // Dividimos por coma seguida de espacio y un dígito (para no romper las descripciones que tengan comas)
    const parts = s.split(/,\s*(?=\d)/);

    return parts.map(part => {
        const trimmed = part.trim();
        const match = trimmed.match(/^(\S+)\s*-\s*(.+)$/);
        if (match) {
            return {
                codigo: match[1].trim(),
                descripcion: normalizeText(match[2].trim())
            };
        }
        // Si no hay código (no hay guion), lo ponemos todo en descripción
        return { codigo: "", descripcion: normalizeText(trimmed) };
    }).filter(o => o.descripcion !== '');
};

// --- Función principal ---

export const processLatestExcel = async (downloadDir: string, tracker?: RunTracker) => {
    try {
        await tracker?.logStep('EXCEL_PROCESS_START', `Buscando archivos en: ${downloadDir}`);
        const files = fs.readdirSync(downloadDir);

        // Filtrar solo los archivos excel
        const excelFiles = files.filter(f => f.endsWith('.xlsx'))
            .map(f => ({
                name: f,
                time: fs.statSync(path.join(downloadDir, f)).mtime.getTime()
            }))
            .sort((a, b) => b.time - a.time);

        if (excelFiles.length === 0) {
            throw new Error('No se encontraron archivos Excel en el directorio.');
        }

        const latestFile = excelFiles[0].name;
        const filePath = path.join(downloadDir, latestFile);

        await tracker?.logStep('FILE_SELECTED', `Procesando el archivo más reciente: ${filePath}`);

        // Leer el archivo de Excel
        const workbook = xlsx.readFile(filePath);

        // Buscar la hoja llamada "resultados"
        const targetSheetName = "resultados";
        const sheetNameFound = workbook.SheetNames.find(
            name => name.toLowerCase() === targetSheetName.toLowerCase()
        );

        if (!sheetNameFound) {
            throw new Error(`No se encontró la pestaña "${targetSheetName}" en el archivo Excel. Pestañas disponibles: ${workbook.SheetNames.join(', ')}`);
        }

        const worksheet = workbook.Sheets[sheetNameFound];
        const jsonData = xlsx.utils.sheet_to_json(worksheet, { defval: "" });

        if (jsonData.length === 0) {
            throw new Error('El archivo Excel no contiene datos en la pestaña de resultados.');
        }

        await tracker?.logStep('DATA_EXTRACTED', `Se han extraído ${jsonData.length} filas del Excel.`);

        // --- TRANSFORMACIÓN DE DATOS ---
        const datosExcel = jsonData.map((item: any) => ({
            codigo: formatDataValue(String(item['CÓDIGO'] || '')),
            denominacion: normalizeText(String(item['DENOMINACIÓN'] || '')),
            version: parseNumber(item['VERSIÓN']),
            familia_profesional: normalizeText(String(item['FAMILIA PROFESIONAL'] || '')),
            area_profesional: normalizeText(String(item['ÁREA PROFESIONAL'] || '')),
            competencia_transversal: parseStringOrNull(item['COMPETENCIA TRANSVERSAL']),
            nivel_cualificacion: parseNumberOrNull(item['NIVEL DE CUALIFICACIÓN']),
            modalidad_imparticion: mapModalidad(String(item['MODALIDAD DE IMPARTICIÓN'] || '')),
            duracion_total: parseNumber(item['DURACIÓN TOTAL']),
            duracion_total_parte_presencial: parseNumberOrNull(item['DURACIÓN TOTAL DE LA PARTE PRESENCIAL (TELEFORMACIÓN O MIXTA)']),
            ocupaciones_relacionadas: parseOcupaciones(String(item['OCUPACIONES RELACIONADAS'] || ''))
        }));

        // --- LÓGICA INCREMENTAL ---
        const totalEnDB = await EspecificacionFormativa.countDocuments();
        const totalEnExcel = datosExcel.length;

        await tracker?.logStep('COMPARISON', `Registros en DB: ${totalEnDB} | Registros en Excel: ${totalEnExcel}`);

        // Si la cantidad es igual, no hay nada nuevo
        if (totalEnExcel <= totalEnDB) {
            await tracker?.logStep('NO_UPDATE_NEEDED', 'La base de datos ya está actualizada. No se encontraron registros nuevos.');
            if (tracker) {
                await tracker.finishSuccess({
                    rowsInExcel: totalEnExcel,
                    rowsInDB: totalEnDB,
                    rowsInserted: 0,
                    message: 'Sin cambios'
                });
            }
            return datosExcel.slice(0, 5);
        }

        // Si el Excel tiene más registros, buscar cuáles faltan
        const codigosEnDB = new Set(
            (await EspecificacionFormativa.find({}, { codigo: 1, _id: 0 }).lean())
                .map(doc => doc.codigo)
        );

        const registrosFaltantes = datosExcel.filter(d => !codigosEnDB.has(d.codigo));

        await tracker?.logStep('DIFF_CALCULATED', `Se encontraron ${registrosFaltantes.length} registros nuevos para insertar.`);

        if (registrosFaltantes.length === 0) {
            await tracker?.logStep('NO_UPDATE_NEEDED', 'Todos los códigos del Excel ya existen en la DB.');
            if (tracker) {
                await tracker.finishSuccess({
                    rowsInExcel: totalEnExcel,
                    rowsInDB: totalEnDB,
                    rowsInserted: 0,
                    message: 'Sin registros nuevos'
                });
            }
            return datosExcel.slice(0, 5);
        }

        // Insertar solo los registros faltantes
        await EspecificacionFormativa.insertMany(registrosFaltantes);
        await tracker?.logStep('DB_INSERT_SUCCESS', `Se insertaron ${registrosFaltantes.length} registros nuevos exitosamente.`);

        // Registrar métricas de éxito
        if (tracker) {
            await tracker.finishSuccess({
                rowsInExcel: totalEnExcel,
                rowsInDB: totalEnDB,
                rowsInserted: registrosFaltantes.length
            });
        }

        return registrosFaltantes.slice(0, 5);

    } catch (error: any) {
        await tracker?.logStep('EXCEL_PROCESS_ERROR', `Error al procesar el Excel o Base de Datos: ${error.message}`);
        throw error;
    }
}
