// Keep one TypeScript path untouched for the mixed source coverage assertion
export function chooseTS(value: boolean) {
  if (value) {
    return 51;
  }
  return -51;
}

export function untouchedTS() {
  return 61;
}
