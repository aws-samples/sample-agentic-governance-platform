import client from '../../api/client';
import { DEVAUTH_HEADERS, DEVAUTH_MODE, DEVAUTH_USER } from './arm';

export type BootStep = 'mount' | 'bridged';

let installed = false;

export async function bootAuth(): Promise<BootStep> {
  if (!installed) {
    client.interceptors.request.use((config) => {
      Object.assign(config.headers, DEVAUTH_HEADERS);
      return config;
    });
    installed = true;
    console.info(`[authBoot] the ${DEVAUTH_MODE} arm signs every request as ${DEVAUTH_USER.username}`);
  }
  return 'mount';
}
