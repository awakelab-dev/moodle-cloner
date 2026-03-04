import request from 'supertest';
import mongoose from 'mongoose';
import { MongoMemoryServer } from 'mongodb-memory-server';
import { app } from '../../server';
import { EspecificacionFormativa } from '../../models/EspecificacionFormativa';
import { Run } from '../../models/Run';

// Mock dependencies that would trigger real browsers or reads
jest.mock('../../descargar_sepe', () => ({
    downloadSepeFile: jest.fn().mockResolvedValue('descargas/mock_excel.xlsx')
}));

jest.mock('../../excelProcessor', () => ({
    processLatestExcel: jest.fn().mockResolvedValue([
        { codigo: 'MOCK01', denominacion: 'Curso Mock' }
    ])
}));

describe('API Integration Tests', () => {
    let mongoServer: MongoMemoryServer;

    beforeAll(async () => {
        // Inicializar in-memory mongo para los tests API
        mongoServer = await MongoMemoryServer.create();
        const uri = mongoServer.getUri();

        // Desconectar cualquier conexión previa
        if (mongoose.connection.readyState !== 0) {
            await mongoose.disconnect();
        }
        await mongoose.connect(uri);
    });

    afterAll(async () => {
        await mongoose.disconnect();
        await mongoServer.stop();
    });

    beforeEach(async () => {
        await EspecificacionFormativa.deleteMany({});
        await Run.deleteMany({});
    });

    describe('GET /api/registros', () => {
        it('should return empty list when no records exist', async () => {
            const res = await request(app).get('/api/registros');
            expect(res.status).toBe(200);
            expect(res.body.success).toBe(true);
            expect(res.body.data).toEqual([]);
        });

        it('should return records up to 50', async () => {
            await EspecificacionFormativa.create({ codigo: 'TEST01', modalidad_imparticion: 'teleformacion' });

            const res = await request(app).get('/api/registros');
            expect(res.status).toBe(200);
            expect(res.body.success).toBe(true);
            expect(res.body.data).toHaveLength(1);
            expect(res.body.data[0].codigo).toBe('TEST01');
        });
    });

    describe('GET /api/runs', () => {
        it('should return historical runs', async () => {
            await Run.create({ runId: 'run-123', status: 'SUCCESS', steps: [] });
            const res = await request(app).get('/api/runs');
            expect(res.status).toBe(200);
            expect(res.body.data).toHaveLength(1);
            expect(res.body.data[0].runId).toBe('run-123');
        });
    });

    describe('GET /api/procesar-sepe', () => {
        it('should execute process successfully using mocks', async () => {
            const res = await request(app).get('/api/procesar-sepe');
            expect(res.status).toBe(200);
            expect(res.body.success).toBe(true);
            expect(res.body.data).toHaveLength(1);
            expect(res.body.data[0].codigo).toBe('MOCK01');
        });
    });
});
