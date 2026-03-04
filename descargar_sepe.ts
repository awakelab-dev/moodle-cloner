import { chromium } from 'playwright';
import * as path from 'path';
import * as fs from 'fs';
import { RunTracker } from './utils/runTracker';

/**
 * Función principal para automatizar la descarga desde la Sede del SEPE.
 * @param tracker Opcional. Instancia de RunTracker para registrar los logs estructurados.
 * @returns {Promise<string | null>} La ruta absoluta del archivo descargado, o null si falla.
 */
export async function downloadSepeFile(tracker?: RunTracker): Promise<string | null> {
    // Usar la carpeta 'descargas' relativa a la ubicación del proyecto en lugar de la ubicación del ejecutable compilado
    const downloadDir = path.resolve(process.cwd(), 'descargas');

    // Crear el directorio /descargas si no existe
    if (!fs.existsSync(downloadDir)) {
        fs.mkdirSync(downloadDir, { recursive: true });
    }

    // Tiempos de espera extendidos (ej. 60 segundos) debido a la carga de datos
    const EXTENDED_TIMEOUT = 60000;

    // Iniciar Navegador en modo headless
    const browser = await chromium.launch({ headless: true });

    // Crear contexto permitiendo descargas
    const context = await browser.newContext({
        acceptDownloads: true
    });

    const page = await context.newPage();

    // Configurar timeouts por defecto en la página para evitar que falle en cargas lentas
    page.setDefaultTimeout(EXTENDED_TIMEOUT);
    page.setDefaultNavigationTimeout(EXTENDED_TIMEOUT);

    try {
        await tracker?.logStep('NAVIGATION', 'Navegando a la página del SEPE...');
        await page.goto('https://sede.sepe.gob.es/FOET_CATALOGO_EEFF_SEDE/flows/main?execution=e1s1');

        await tracker?.logStep('SEARCH', 'Buscando el botón de BUSCAR y haciendo clic...');
        // ==========================================
        // SELECTOR_BOTON_BUSCAR: Reemplazar con el selector real
        // Ejemplo genérico: 'button:has-text("BUSCAR")', 'input[value="BUSCAR"]', o un ID '#btnBuscar'
        // ==========================================
        const selectorBtnBuscar = '#formulario\\:bBuscar';
        await page.click(selectorBtnBuscar);

        await tracker?.logStep('WAIT_RESULTS', 'Esperando a que los resultados se carguen por completo...');
        // ==========================================
        // SELECTOR_TABLA_RESULTADOS: Reemplazar con el selector de la tabla o del contenedor de resultados visible
        // ==========================================
        const selectorTablaResultados = '#formGeneral\\:resultadoEspecialidadesTablaJSF';
        await page.waitForSelector(selectorTablaResultados, { state: 'visible', timeout: EXTENDED_TIMEOUT });

        await tracker?.logStep('EXPORT_CLICK', 'Haciendo clic al botón "Exportar"...');
        // ==========================================
        // SELECTOR_BOTON_EXPORTAR: Reemplazar con el selector del botón de Exportar
        // ==========================================
        const selectorBtnExportar = '#exportar';
        await page.click(selectorBtnExportar);

        await tracker?.logStep('WAIT_MODAL', 'Esperando a que aparezca la ventana modal...');
        // ==========================================
        // SELECTOR_MODAL: Reemplazar con el selector del contenedor de la ventana modal
        // ==========================================
        const selectorModal = '#formulario\\:modalExportar'; // Contenedor del modal
        await page.waitForSelector(selectorModal, { state: 'visible' });

        await tracker?.logStep('CHECKBOXES', 'Marcando los checkboxes dentro de la modal...');
        // ==========================================
        // SELECTORES_CHECKBOXES: Reemplazar con los selectores exactos de los 3 checkboxes a marcar
        // ==========================================
        const selectorCheckbox1 = '#formulario\\:modalExportar\\:checkboxExportar\\:checkboxExportarSelect\\:0';
        const selectorCheckbox2 = '#formulario\\:modalExportar\\:checkboxExportar\\:checkboxExportarSelect\\:1';
        const selectorCheckbox3 = '#formulario\\:modalExportar\\:checkboxExportar\\:checkboxExportarSelect\\:2';

        // page.check() marca la casilla si no está marcada, esperando que sea clickeable.
        await page.check(selectorCheckbox1);
        await page.check(selectorCheckbox2);
        await page.check(selectorCheckbox3);

        await tracker?.logStep('DOWNLOAD_START', 'Preparando intercepción de la descarga y haciendo clic en Continuar...');
        // ==========================================
        // SELECTOR_BOTON_CONTINUAR: Reemplazar con el selector del botón "Continuar" de la modal
        // ==========================================
        const selectorBtnContinuar = '#formulario\\:modalExportar\\:botonImprimir';

        // Es IMPORTANTE iniciar la espera del evento 'download' ANTES de hacer el clic que lo dispara.
        const downloadPromise = page.waitForEvent('download', { timeout: EXTENDED_TIMEOUT });
        await page.click(selectorBtnContinuar);

        await tracker?.logStep('DOWNLOAD_WAIT', 'Esperando a que finalice la descarga del archivo...');
        const download = await downloadPromise;

        // Playwright automáticamente detecta el nombre del archivo (ej: ExcelTablaConsulta-YYYYMMDD_HHMMSS.xlsx)
        const suggestedFilename = download.suggestedFilename();
        const finalFilePath = path.join(downloadDir, suggestedFilename);

        // Guardar el archivo desde el stream temporal hasta nuestra ruta final.
        // El await garantiza que se complete la escritura en el disco.
        await download.saveAs(finalFilePath);

        await tracker?.logStep('DOWNLOAD_SUCCESS', `Descarga completada con éxito. Archivo guardado en: ${finalFilePath}`);
        return finalFilePath;

    } catch (error: any) {
        await tracker?.logStep('PLAYWRIGHT_ERROR', `Ocurrió un error en la automatización: ${error.message}`);
        throw error; // Lanzamos el error para que server.ts lo maneje y marque la corrida como fallida
    } finally {
        await tracker?.logStep('BROWSER_CLOSE', 'Cerrando el navegador de forma segura...');
        // Siempre cerrar el navegador para evitar procesos "zombis"
        await browser.close();
    }
}

// Ejecución del script solo si se llama directamente
if (require.main === module) {
    downloadSepeFile().then(resultado => {
        if (resultado) {
            console.log('\n--- PROCESO FINALIZADO ---');
            console.log('Ruta final:', resultado);
        } else {
            console.log('\n--- PROCESO FINALIZADO CON ERRORES ---');
        }
    });
}
