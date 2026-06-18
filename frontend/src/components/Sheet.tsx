import * as Dialog from "@radix-ui/react-dialog";
import styles from "./components.module.css";

/** A bottom sheet built on Radix Dialog — correct focus trapping + escape/overlay dismissal,
 * our own styling. */
export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: React.ReactNode;
}): React.JSX.Element {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.sheet} data-testid="sheet">
          <div className={styles.grabber} aria-hidden="true" />
          <Dialog.Title className={styles.sheetTitle}>{title}</Dialog.Title>
          {description ? (
            <Dialog.Description className="muted">{description}</Dialog.Description>
          ) : (
            <Dialog.Description className="sr-only">{title}</Dialog.Description>
          )}
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
