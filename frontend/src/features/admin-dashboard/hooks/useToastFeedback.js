import { useCallback, useState } from 'react';
import toast from 'react-hot-toast';

// Keep contextual validation/retry text while announcing operation results.
// Stable IDs update repeated failures instead of stacking identical toasts.
export default function useToastFeedback(kind = 'error') {
  const [message, setMessage] = useState('');
  const feedback = useCallback((value) => {
    setMessage(value);
    if (value) toast[kind](value, { id: `admin:${kind}:${value}` });
  }, [kind]);
  return [message, feedback];
}
