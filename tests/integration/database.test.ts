import mongoose from 'mongoose';
import { MongoMemoryServer } from 'mongodb-memory-server';
import { EspecificacionFormativa, Modalidad } from '../../models/EspecificacionFormativa';

describe('Database Integration', () => {
    let mongoServer: MongoMemoryServer;

    beforeAll(async () => {
        mongoServer = await MongoMemoryServer.create();
        const uri = mongoServer.getUri();
        await mongoose.connect(uri);
    });

    afterAll(async () => {
        await mongoose.disconnect();
        await mongoServer.stop();
    });

    beforeEach(async () => {
        await EspecificacionFormativa.deleteMany({});
    });

    it('should insert new valid records', async () => {
        const records = [
            {
                codigo: 'COMP01',
                denominacion: 'Programación Base',
                modalidad_imparticion: 'mixta' as Modalidad
            }
        ];

        await EspecificacionFormativa.insertMany(records);
        const count = await EspecificacionFormativa.countDocuments();
        expect(count).toBe(1);

        const item = await EspecificacionFormativa.findOne({ codigo: 'COMP01' });
        expect(item?.denominacion).toBe('Programación Base');
        expect(item?.modalidad_imparticion).toBe('mixta');
    });

    it('should correctly handle schema defaults and validations', async () => {
        const minimalRecord = new EspecificacionFormativa({
            codigo: 'COMP02',
            modalidad_imparticion: 'teleformacion' as Modalidad
        });

        const saved = await minimalRecord.save();
        expect(saved.version).toBe(0); // Defaults to 0
        expect(saved.competencia_transversal).toBeNull(); // Defaults to null
        expect(saved.ocupaciones_relacionadas).toEqual([]); // Defaults to empty array
    });

    it('should throw validation error if required fields are missing', async () => {
        const invalidRecord = new EspecificacionFormativa({
            denominacion: 'Falta código y modalidad'
        });

        let error: any;
        try {
            await invalidRecord.save();
        } catch (e) {
            error = e;
        }

        expect(error).toBeDefined();
        expect(error.name).toBe('ValidationError');
    });

    it('should enforce unique constraint on codigo', async () => {
        const record = {
            codigo: 'DUPLICATE01',
            modalidad_imparticion: 'presencial' as Modalidad
        };

        await EspecificacionFormativa.create(record);

        let error: any;
        try {
            await EspecificacionFormativa.create(record);
        } catch (e) {
            error = e;
        }

        expect(error).toBeDefined();
        // Mongoose 11000 is duplicate key error code from underlying mongodb driver
        expect(error.code).toBe(11000);
    });
});
