import React from "react";

const LoadingScreen = function() {
  return (
    <div className='text-center'>
      <div className="lds-ripple"><div></div><div></div></div>
      <p className="heading has-text-primary">Loading...</p>
    </div>
  )
};

export default LoadingScreen;
