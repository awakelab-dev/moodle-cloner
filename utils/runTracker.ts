import { Run } from '../models/Run';
import * as crypto from 'crypto';

export class RunTracker {
    public runId: string;
    private startTime: number;

    constructor() {
        this.runId = crypto.randomUUID();
        this.startTime = Date.now();
    }

    async start() {
        await Run.create({
            runId: this.runId,
            status: 'RUNNING',
            startTime: new Date(this.startTime),
            steps: [{ name: 'INIT', timestamp: new Date(), message: 'Corrida iniciada.' }]
        });
        console.log(`[RUN ${this.runId}] Iniciado`);
    }

    async logStep(name: string, message?: string) {
        console.log(`[RUN ${this.runId}] [${name}] ${message || ''}`);
        await Run.updateOne(
            { runId: this.runId },
            { $push: { steps: { name, timestamp: new Date(), message } } }
        );
    }

    async finishSuccess(metrics?: any) {
        const endTime = Date.now();
        const durationMs = endTime - this.startTime;

        console.log(`[RUN ${this.runId}] Completado exitosamente en ${durationMs}ms`);
        await Run.updateOne(
            { runId: this.runId },
            {
                status: 'SUCCESS',
                endTime: new Date(endTime),
                durationMs,
                metrics
            }
        );
    }

    async finishError(error: any) {
        const endTime = Date.now();
        const durationMs = endTime - this.startTime;

        console.error(`[RUN ${this.runId}] Fallido en ${durationMs}ms:`, error);
        await Run.updateOne(
            { runId: this.runId },
            {
                status: 'FAILED',
                endTime: new Date(endTime),
                durationMs,
                errorContext: {
                    type: error.name || 'UnknownError',
                    message: error.message || String(error),
                    stack: error.stack
                }
            }
        );
    }
}
