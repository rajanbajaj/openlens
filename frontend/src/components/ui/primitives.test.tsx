import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Badge } from './Badge.tsx';
import { Button } from './Button.tsx';
import { Card } from './Card.tsx';
import { Chip } from './Chip.tsx';
import { Drawer } from './Drawer.tsx';
import { Select } from './Select.tsx';
import { Skeleton } from './Skeleton.tsx';
import { TextInput } from './TextInput.tsx';
import { Toasts } from './Toast.tsx';

afterEach(() => {
  cleanup();
});

describe('ui primitives', () => {
  it('Button renders variants and forwards clicks', () => {
    const onClick = vi.fn();
    render(
      <>
        <Button variant="primary" onClick={onClick}>
          Go
        </Button>
        <Button variant="ghost" disabled>
          Quiet
        </Button>
      </>,
    );
    const go = screen.getByRole('button', { name: 'Go' });
    expect(go).toHaveClass('ol-button', 'ol-button-primary');
    fireEvent.click(go);
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Quiet' })).toBeDisabled();
  });

  it('TextInput pairs a visible label with its input', () => {
    render(<TextInput label="Nickname" id="nick" value="" onChange={() => {}} />);
    expect(screen.getByLabelText('Nickname')).toHaveAttribute('id', 'nick');
  });

  it('Select offers options and reports the chosen value', () => {
    const onChange = vi.fn();
    render(
      <Select
        label="Fruit"
        value="apple"
        options={[
          { value: 'apple', label: 'Apple' },
          { value: 'pear', label: 'Pear' },
        ]}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText('Fruit'), { target: { value: 'pear' } });
    expect(onChange).toHaveBeenCalledWith('pear');
  });

  it('Chip renders static text and an optional remove control', () => {
    const onRemove = vi.fn();
    render(
      <>
        <Chip>plain</Chip>
        <Chip onRemove={onRemove} removeLabel="Drop it">
          removable
        </Chip>
      </>,
    );
    expect(screen.getByText('plain')).toHaveClass('ol-chip');
    fireEvent.click(screen.getByRole('button', { name: 'Drop it' }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('Card renders its children in a card container', () => {
    render(<Card data-testid="card">hello</Card>);
    expect(screen.getByTestId('card')).toHaveClass('ol-card');
    expect(screen.getByText('hello')).toBeInTheDocument();
  });

  it('Drawer toggles its panel with an expanded trigger', () => {
    const onToggle = vi.fn();
    const { rerender } = render(
      <Drawer
        triggerLabel="Show details"
        open={false}
        onToggle={onToggle}
        panelId="panel"
        panelTestId="panel"
      >
        body
      </Drawer>,
    );
    const trigger = screen.getByRole('button', { name: 'Show details' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('panel')).not.toBeInTheDocument();
    fireEvent.click(trigger);
    expect(onToggle).toHaveBeenCalledTimes(1);
    rerender(
      <Drawer
        triggerLabel="Hide details"
        open
        onToggle={onToggle}
        panelId="panel"
        panelTestId="panel"
      >
        body
      </Drawer>,
    );
    expect(screen.getByRole('button', { name: 'Hide details' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByTestId('panel')).toHaveTextContent('body');
  });

  it('Badge renders an inline label', () => {
    render(<Badge>mock</Badge>);
    expect(screen.getByText('mock')).toHaveClass('ol-badge');
  });

  it('Skeleton renders decorative bars hidden from assistive tech', () => {
    render(<Skeleton bars={2} />);
    const skeleton = screen.getByTestId('skeleton');
    expect(skeleton).toHaveAttribute('aria-hidden', 'true');
    expect(skeleton.children).toHaveLength(2);
  });

  it('Toasts announces messages politely and renders nothing when empty', () => {
    const { rerender } = render(<Toasts items={[]} />);
    expect(screen.queryByTestId('toasts')).not.toBeInTheDocument();
    rerender(<Toasts items={[{ id: 't1', text: 'Saved' }]} />);
    const region = screen.getByTestId('toasts');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toHaveTextContent('Saved');
  });
});
