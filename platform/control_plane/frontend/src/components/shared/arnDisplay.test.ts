import { describe, expect, it } from 'vitest';
import { maskArn } from './arnDisplay';

const RUNTIME = 'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/fnol-abc123';

describe('maskArn — no account id is printed anywhere, so a displayed ARN shows only the last four digits of its account', () => {
  it('masks the account id, keeping its last four digits', () => {
    expect(maskArn(RUNTIME)).toBe('arn:aws:bedrock-agentcore:us-east-1:••••3333:runtime/fnol-abc123');
  });

  it('masks a region-less ARN (IAM)', () => {
    expect(maskArn('arn:aws:iam::111122223333:role/deploy')).toBe('arn:aws:iam::••••3333:role/deploy');
  });

  it('masks every ARN inside a longer string', () => {
    const two = `${RUNTIME} and arn:aws-us-gov:iam::444455556666:role/x`;
    expect(maskArn(two)).toBe(
      'arn:aws:bedrock-agentcore:us-east-1:••••3333:runtime/fnol-abc123 and arn:aws-us-gov:iam::••••6666:role/x',
    );
  });

  it('leaves an ARN without an account id unchanged (S3)', () => {
    expect(maskArn('arn:aws:s3:::my-bucket/key')).toBe('arn:aws:s3:::my-bucket/key');
  });

  it('leaves non-ARN strings unchanged', () => {
    expect(maskArn('https://gw.example.com/mcp')).toBe('https://gw.example.com/mcp');
    expect(maskArn('')).toBe('');
  });

  it('keeps two accounts distinguishable after masking', () => {
    expect(maskArn('arn:aws:iam::111122223333:role/x')).not.toBe(maskArn('arn:aws:iam::444455556666:role/x'));
  });

  it('does not mask a 12-digit run that is not the account field', () => {
    expect(maskArn('arn:aws:sqs:us-east-1:111122223333:q-999988887777')).toBe(
      'arn:aws:sqs:us-east-1:••••3333:q-999988887777',
    );
  });
});
