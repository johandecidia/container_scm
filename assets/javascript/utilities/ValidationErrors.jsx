import React from 'react';

export default function(props) {
  if (props.errors) {
    return (
      <p className="text-sm text-base-content/70 text-error">
        { props.errors.map((error, i) => {
          return <span key={i}>{error}</span>
        })}
      </p>
    );
  }
  return '';
};
